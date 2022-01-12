from itertools import product
import unittest

import torch
from torch.testing._internal.common_utils import TEST_CUDA
from torch.testing._internal.jit_utils import JitTestCase

try:
    from torchvision import models
except ImportError:
    models = None

if __name__ == "__main__":
    raise RuntimeError(
        "This test file is not meant to be run directly, use:\n\n"
        "\tpython test/test_jit.py TESTNAME\n\n"
        "instead."
    )

# TODO: Delete this when PR #67786 is merged.
def apply_input_props_using_example(graph, example_input):
    """
    Applies properties for each tensor in the graph inputs
    using the example supplied.
    """
    graph_inputs = list(graph.inputs())
    if len(graph_inputs) == 0:
        return

    # Strip self args off for methods
    in_0 = graph_inputs[0]
    if isinstance(in_0.type(), torch._C.ClassType) and in_0.debugName() == "self":
        graph_inputs = graph_inputs[1:]

    if not len(graph_inputs) == len(example_input):
        raise RuntimeError(
            "Number of inputs in graph does not match number of inputs in the example"
        )

    for i, (graph_i, example_i) in enumerate(zip(graph_inputs, example_input)):
        if example_i is None:
            continue  # Skip the type check

        if isinstance(example_i, torch.Tensor) != isinstance(
            graph_i.type(), torch.TensorType
        ):
            raise RuntimeError(
                f"Input {i} does not match type of example", graph_i, example_i
            )

        if isinstance(example_i, torch.Tensor):
            graph_i.setType(torch.TensorType.create_from_tensor(example_i))  # type: ignore[arg-type]


class TestDeviceAnalysis(JitTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cpu = torch.device("cpu")
        cls.cuda = torch.device("cuda")
        cls.vulkan = torch.device("vulkan")
        cls.mkldnn = torch.device("mkldnn")
        cls.device_types = [cls.cpu, cls.cuda, cls.vulkan]

    @staticmethod
    def node_output_device(graph):
        graph_out = list(graph.outputs())
        assert len(graph_out) == 1
        return graph_out[0].type().device()

    def prop_device_on_graph(self, graph, example_devices, in_shapes=None):
        graph_inputs = list(graph.inputs())
        torch._C._jit_pass_erase_shape_information(graph)

        self.assertEqual(len(graph_inputs), len(example_devices))
        for graph_i, device_i in zip(graph_inputs, example_devices):
            if device_i is not None:
                graph_i.setType(graph_i.type().with_device(device_i))

        if in_shapes:
            for graph_i, shapes_i in zip(graph_inputs, in_shapes):
                if shapes_i is not None:
                    graph_i.setType(graph_i.type().with_sizes(shapes_i))

            torch._C._jit_pass_propagate_shapes_on_graph(graph)

        torch._C._jit_pass_propagate_device(graph)

    def assert_device_equal(
        self, fn, in_devices, expected_device, in_shapes=None, subtest_str=""
    ):
        with self.subTest(
            f"In device: {in_devices}, expected: {expected_device}, \n {subtest_str}"
        ):
            graph = torch.jit.script(fn).graph
            self.prop_device_on_graph(graph, in_devices, in_shapes)
            actual_device = self.node_output_device(graph)

            if expected_device is None or actual_device is None:
                self.assertEqual(actual_device, expected_device)
            else:
                self.assertEqual(
                    actual_device.type, expected_device.type, "Failed Verification"
                )

    def test_device_apply(self):
        # Test if the device is properly applied to the input
        def add_self(x):
            return x + x

        graph = torch.jit.script(add_self).graph
        graph_input = next(graph.inputs())
        graph_input.setType(graph_input.type().with_device(self.cpu))
        # self.prop_device_on_graph(graph, [self.cpu])
        self.assertEqual(graph_input.type().device(), self.cpu)

    @unittest.skipIf(models is None, "Requires torchvision")
    def test_mobilenet(self):
        in_cpu = torch.randn(1, 3, 224, 224, device=self.cpu)
        in_example = in_cpu

        expected_device = self.cpu
        m = torch.jit.script(models.mobilenet_v3_small())
        m.eval()
        graph = torch.jit.freeze(m).graph
        # torch._C._jit_pass_erase_shape_information(graph)
        apply_input_props_using_example(graph, in_example)
        torch._C._jit_pass_propagate_shapes_on_graph(graph)
        torch._C._jit_pass_propagate_device(graph)

        actual_device = self.node_output_device(graph)

        if expected_device is None or actual_device is None:
            self.assertEqual(actual_device, expected_device)
        else:
            self.assertEqual(
                actual_device.type, expected_device.type, "Failed Verification"
            )

    def test_simple(self):
        def add_self(x):
            return x + x

        def relu_(x):
            return torch.nn.functional.relu_(x)

        functions = [add_self, relu_]

        for in_device, fn in product(self.device_types, functions):
            self.assert_device_equal(fn, [in_device], in_device)

    def test_set_dtype(self):
        def set_device(x):
            return x.to("cpu")

        for in_device in self.device_types:
            self.assert_device_equal(set_device, [in_device], self.cpu)

    def test_device_arg(self):
        # Test that no device gets propagated when arg is passed in
        def set_device(x, device_name: torch.device):
            return x.to(device=device_name)

        for in_device in self.device_types:
            self.assert_device_equal(set_device, [in_device, None], None)

    def zerodim_test_core(self, device_pairs):
        # Test the support of zerodim tensors with non-zerodim tensors
        def mul(x, y):
            return x * y

        def add(x, y):
            return x + y

        fns = [mul, add]

        input_shapes = [
            ((1, 2, 2), (2, 2)),  # Different dim, non-zerodim
            ((1, 2, 2), ()),  # one zerodim
            ((), ()),  # both zerodim
        ]

        for fn, shapes, devices in product(fns, input_shapes, device_pairs):
            subtest_str = f"{fn.__name__} \n shapes: {shapes}, \n devices: {devices}"
            in0 = torch.rand(shapes[0], device=devices[0])
            in1 = torch.rand(shapes[1], device=devices[1])

            try:
                out = fn(in0, in1)
            except Exception as e:
                # Don't expect eager failures for CPU zerodim tensors
                for i in range(len(devices)):
                    if shapes[i] == () and devices[i] == self.cpu:
                        raise e

                # only expect eager failures on different devices
                if devices[0] == devices[1]:
                    raise e

                # Expect result device to be None for the failure cases.
                self.assert_device_equal(fn, devices, None, shapes, subtest_str)
                continue

            self.assert_device_equal(fn, devices, out.device, shapes, subtest_str)

            # Test that without shapes, we either get the same device or None for the device
            # Aka that the code is convservative for tensor shapes.
            graph = torch.jit.script(fn).graph
            self.prop_device_on_graph(graph, devices)
            actual_device = self.node_output_device(graph)
            self.assertTrue(
                (actual_device is None) or (actual_device.type == out.device.type)
            )

    def test_zerodim_cpu(self):
        # Allow for minimal testing locally
        self.zerodim_test_core([(self.cpu, self.cpu)])

    def test_zerodim_no_device(self):
        # If device is missing, you should never be able to infer device type.
        def mul(x, y):
            return x * y

        def add(x, y):
            return x + y

        fns = [mul, add]

        device_pairs = [
            (self.cpu, None),
            (None, self.cpu),
            (None, None),
        ]

        input_shapes = [
            ((1, 2, 2), (2, 2)),  # Different dim, non-zerodim
            ((1, 2, 2), ()),  # one zerodim
            ((), ()),  # both zerodim
        ]

        for fn, shapes, devices in product(fns, input_shapes, device_pairs):
            self.assert_device_equal(fn, devices, None, shapes)

    @unittest.skipIf(not TEST_CUDA, "No CUDA")
    def test_zerodim_gpu(self):
        device_pairs = [
            (self.cpu, self.cuda),
            (self.cuda, self.cpu),
            (self.cuda, self.cuda),
        ]
        self.zerodim_test_core(device_pairs)

    def test_device_if_propagation(self):
        def test_fn(x, y, z: bool):
            if z:
                return x + 3
            else:
                return y * 2

        self.assert_device_equal(test_fn, [self.cpu, self.cpu, None], self.cpu)
        self.assert_device_equal(test_fn, [self.mkldnn, self.mkldnn, None], self.mkldnn)
        self.assert_device_equal(test_fn, [self.cpu, self.cuda, None], None)