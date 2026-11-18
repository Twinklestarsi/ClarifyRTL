class _Enc:
    def encode(self, text, *a, **k): return list(range(max(1, len(text) // 4)))
def encoding_for_model(model): return _Enc()
def get_encoding(name): return _Enc()
