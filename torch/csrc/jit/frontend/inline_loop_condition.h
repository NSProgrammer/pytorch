#pragma once
#include <functional>
#include <memory>
#include <string>

#include <torch/csrc/Export.h>
#include <torch/csrc/jit/ir/ir.h>

namespace torch {
namespace jit {

TORCH_API void inlineLoopCondition(std::shared_ptr<Graph>& graph);
TORCH_API void InlineBlockBeforeNode(Node* before_node, Block* block);

} // namespace jit
} // namespace torch
