"""Block 3: spec -> IR -> representability closure -> frozen IR_exp."""

from .checks import check_ir, generate_initial_ir, ir_digest, load_ir_model
from .inventory import plan_questions, validate_question
from .closure import close_sites

__all__ = ["check_ir", "generate_initial_ir", "ir_digest", "load_ir_model",
           "plan_questions", "validate_question", "close_sites"]
