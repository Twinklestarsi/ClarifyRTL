"""Block 2: the question-site model (S0-S3) and its mechanical detector."""

from .schema import Option, Question, SiteVerdict, S0, S1, S2, S3, STATUSES
from .patches import apply_patch, dump_ir, load_ir, structural_diff, within_site
from .detect import check_candidate_text, detect_site, summarise

__all__ = ["Option", "Question", "SiteVerdict", "S0", "S1", "S2", "S3", "STATUSES",
           "apply_patch", "dump_ir", "load_ir", "structural_diff", "within_site",
           "check_candidate_text", "detect_site", "summarise"]
