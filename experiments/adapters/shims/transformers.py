class AutoTokenizer:
    @classmethod
    def from_pretrained(cls, *a, **k): return cls()
class AutoModelForCausalLM:
    @classmethod
    def from_pretrained(cls, *a, **k): return cls()
class BitsAndBytesConfig:
    def __init__(self, *a, **k): pass
