"""GLM-5.3-Flash prefill FLOP numerator.

Adapted from glm5f-dsub@148a1202, glm5f/glm5f_flops.py and its documented
FLOP convention. This model adapter contains no hardware peak or MFU denominator.

Shapes and layer types come from the supplied model configuration and are
asserted against what 口径.md states: 34 KDA, 11 DSA, 3 dense, 42 MoE layers, 288 experts choose 8
plus 1 shared (design §9 lesson 3: ds41's MFU baseline once used wrong layer counts). Counts follow
the reference form of modeling_glm5_next.py (口径.md §1): non-absorbed DSA, KDA in chunks of 64,
every elementwise op at the §1 constants; no padding, recomputation, MTP, integer ops or lookups.

Each term carries two tags: `row`, the §2.5 table row, and `stage`, the gf_engine stage
(glm5f/c/gf_engine.c gf_engine_stage_names) whose time it is measured in, so G3.x per-stage
times turn into per-stage MFU. The kv_b projection is split between the stages that do its work
in the engine's absorbed form: the key half (W_UK, into q) in dsa_q, the value half (W_UV) in
dsa_attn. The head counts once a request whenever the layer range ends at the last layer.

The public plugin entry point validates the workload and dispatches this counter.
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from fractions import Fraction


def load_text_config(value):
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_dir():
            path = path / 'config.json'
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('GLM model configuration must be an object')
    return value.get('text_config', value)


# 口径.md §1: FLOP per element
SIGMOID, SILU, SOFTMAX = 3, 4, 6
RMSNORM, RMSNORM_UNWEIGHTED, LAYERNORM, GATE, L2NORM, SWIGLU, ADD = 4, 3, 5, 4, 3, 8, 1
CHUNK = 64                      # §1 / §2.3: KDA prefill counted in the reference's chunks of 64
# what 口径.md states the config holds; load_shape refuses any other model
EXPECT = {"layers": 45, "kda": 34, "dsa": 11, "dense": 3, "moe": 42, "n_experts": 288, "top_k": 8, "n_shared": 1}
KDA, DSA = "linear_attention", "deepseek_sparse_attention"
# print orders: the §2.5 rows; the engine stages as a layer runs them
ROWS = ("kda_proj", "kda_core", "kda_elem", "dsa_proj", "dsa_attn", "idx_proj", "idx_score", "mhc", "rmsnorm", "dense_mlp",
        "router", "routed", "shared", "head")
STAGES = ("hc_a", "kda_proj", "kda_fb_conv", "kda_core", "kda_oproj", "dsa_qkv", "dsa_q", "dsa_index", "dsa_attn", "dsa_oproj",
          "attn_exit", "hc_f", "router", "gate_up", "swiglu_ag", "down", "comb_acc", "ffn_exit", "head")


@dataclass(frozen=True)
class Shape:
    hidden: int
    layer_types: tuple
    mlp_types: tuple
    kda_heads: int
    kda_dim: int
    conv: int
    dsa_heads: int
    qk_head: int
    v_head: int
    q_lora: int
    kv_lora: int
    idx_heads: int
    idx_dim: int
    kpool: int
    topk: int
    moe_inter: int
    n_experts: int
    top_k: int
    n_shared: int
    dense_inter: int
    vocab: int
    hc: int
    sinkhorn: int

    kda_layers = property(lambda s: [l for l, t in enumerate(s.layer_types) if t == KDA])
    dsa_layers = property(lambda s: [l for l, t in enumerate(s.layer_types) if t == DSA])
    dense_layers = property(lambda s: [l for l, t in enumerate(s.mlp_types) if t == "dense"])
    moe_layers = property(lambda s: [l for l, t in enumerate(s.mlp_types) if t == "sparse"])


def _need(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(f"config.json: {what}")


def load_shape(cfg) -> Shape:
    """Shape from a config.json path, a model dir, or its parsed dict; raises ValueError unless it is GLM-5.3-Flash."""
    t = load_text_config(cfg)
    try:
        la = t["linear_attn_config"]
        s = Shape(t["hidden_size"], tuple(t["layer_types"]), tuple(t["mlp_layer_types"]), la["num_heads"], la["head_dim"],
                  la["short_conv_kernel_size"], t["num_attention_heads"], t["qk_head_dim"], t["v_head_dim"], t["q_lora_rank"],
                  t["kv_lora_rank"], t["index_n_heads"], t["index_head_dim"], t["index_kpool"], t["index_topk"],
                  t["moe_intermediate_size"], t["n_routed_experts"], t["num_experts_per_tok"], t["n_shared_experts"],
                  t["intermediate_size"], t["vocab_size"], t["hc_mult"], t["hc_sinkhorn_iters"])
        n = t["num_hidden_layers"]
        for name, value in vars(s).items():
            if name not in ('layer_types', 'mlp_types'):
                _need(type(value) is int and value > 0, name + ' must be a positive integer')
        _need(type(n) is int and n > 0, 'num_hidden_layers must be a positive integer')
        first_dense = t["first_k_dense_replace"]
        _need(type(first_dense) is int and 0 <= first_dense <= n, 'first_k_dense_replace must fit the layer range')
        _need(n == len(s.layer_types) == len(s.mlp_types) == len(t["indexer_types"]), "layer lists disagree with num_hidden_layers")
        _need(set(s.layer_types) <= {KDA, DSA} and set(s.mlp_types) <= {"dense", "sparse"}, "unknown layer type")
        _need(la["kda_layers"] == s.kda_layers and la["full_attn_layers"] == s.dsa_layers, "kda_layers / full_attn_layers vs layer_types")
        _need(s.dense_layers == list(range(t["first_k_dense_replace"])), "dense layers are not the first first_k_dense_replace")
        got = {"layers": n, "kda": len(s.kda_layers), "dsa": len(s.dsa_layers), "dense": len(s.dense_layers),
               "moe": len(s.moe_layers), "n_experts": s.n_experts, "top_k": s.top_k, "n_shared": s.n_shared}
        _need(got == EXPECT, f"{got} is not 口径.md's {EXPECT}")
        # forms the formulas assume; anything else needs new terms
        _need(t["qk_rope_head_dim"] == 0 and t["qk_nope_head_dim"] == s.qk_head, "RoPE dims (the text model has none)")
        _need(t["num_key_value_heads"] == s.dsa_heads, "grouped KV heads")
        _need(set(t["indexer_types"]) == {"full"}, "shared-indexer layers")
        _need(t["index_kpool_always_select_tail"] is True and s.topk % s.kpool == 0, "top-k without the tail pool")
        _need(t["mhc"] is True, "no mHC")
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"config.json text_config lacks {e}") from None
    return s


@dataclass(frozen=True)
class Mat:
    """A per-token matmul [n_in] -> [n_out], done per_token times (8 for the routed experts)."""
    category: str       # glm5f_census category of the weight
    store: str          # which stored tensor (the census cross-check's dtype key)
    name: str
    n_out: int
    n_in: int
    per_token: int
    row: str
    stage: str


@dataclass(frozen=True)
class Term:
    row: str
    stage: str
    name: str
    flop: float         # per token


def matmuls(s: Shape, layer: int) -> list[Mat]:
    H, hcw = s.hidden, (2 + s.hc) * s.hc
    m = [Mat("hc", "hc", "hc_attn_fn", hcw, s.hc * H, 1, "mhc", "hc_a"),
         Mat("hc", "hc", "hc_ffn_fn", hcw, s.hc * H, 1, "mhc", "hc_f")]
    if s.layer_types[layer] == KDA:
        qkv, a = s.kda_heads * s.kda_dim, "linear_attention"
        m += [Mat(a, "kda", n, qkv, H, 1, "kda_proj", "kda_proj") for n in ("q", "k", "v")]
        m += [Mat(a, "kda", "f_a", s.kda_dim, H, 1, "kda_proj", "kda_proj"), Mat(a, "kda", "g_a", s.kda_dim, H, 1, "kda_proj", "kda_proj"),
              Mat(a, "kda", "b", s.kda_heads, H, 1, "kda_proj", "kda_proj"),
              Mat(a, "kda", "f_b", qkv, s.kda_dim, 1, "kda_proj", "kda_fb_conv"), Mat(a, "kda", "g_b", qkv, s.kda_dim, 1, "kda_proj", "kda_fb_conv"),
              Mat(a, "kda", "o_kda", H, qkv, 1, "kda_proj", "kda_oproj")]
    else:
        a, i, nq, nv = "attention", "indexer", s.dsa_heads * s.qk_head, s.dsa_heads * s.v_head
        m += [Mat(a, "q_a", "q_a", s.q_lora, H, 1, "dsa_proj", "dsa_qkv"), Mat(a, "kv_a", "kv_a", s.kv_lora, H, 1, "dsa_proj", "dsa_qkv"),
              Mat(a, "q_b", "q_b", nq, s.q_lora, 1, "dsa_proj", "dsa_q"), Mat(a, "kv_b", "kv_b_k", nq, s.kv_lora, 1, "dsa_proj", "dsa_q"),
              Mat(a, "kv_b", "kv_b_v", nv, s.kv_lora, 1, "dsa_proj", "dsa_attn"), Mat(a, "o_dsa", "o_dsa", H, nv, 1, "dsa_proj", "dsa_oproj"),
              Mat(i, "idx", "idx_wq_b", s.idx_heads * s.idx_dim, s.q_lora, 1, "idx_proj", "dsa_index"),
              Mat(i, "idx", "idx_wk", s.idx_dim, H, 1, "idx_proj", "dsa_index"),
              Mat(i, "idx", "idx_pool_gate", s.idx_dim, H, 1, "idx_proj", "dsa_index"),
              Mat(i, "idx", "idx_weights", s.idx_heads, H, 1, "idx_proj", "dsa_index")]
    if s.mlp_types[layer] == "sparse":
        e, sh = s.moe_inter, s.moe_inter * s.n_shared
        m += [Mat("gate", "gate", "router", s.n_experts, H, 1, "router", "router"),
              Mat("routed_experts", "expert", "expert_gate_up", 2 * e, H, s.top_k, "routed", "gate_up"),
              Mat("routed_experts", "expert", "expert_down", H, e, s.top_k, "routed", "down"),
              Mat("shared_experts", "shared", "shared_gate_up", 2 * sh, H, 1, "shared", "gate_up"),
              Mat("shared_experts", "shared", "shared_down", H, sh, 1, "shared", "down")]
    else:
        m += [Mat("dense_mlp", "dense", "dense_gate_up", 2 * s.dense_inter, H, 1, "dense_mlp", "gate_up"),
              Mat("dense_mlp", "dense", "dense_down", H, s.dense_inter, 1, "dense_mlp", "down")]
    return m


def chunk_core_per_head(s: Shape) -> float:
    """§2.3: one head's chunk-form KDA core per token, the per-chunk steps divided by C."""
    C, d = CHUNK, s.kda_dim
    chunk = (C * (C - 1) * d                       # A_kk, strictly lower
             + Fraction((C - 1) * C * (2 * C - 1), 3)  # (I + A)^-1, the reference's row loop
             + 2 * C * (C + 1) * d                 # U = T (v b), W = T (k b e^g)
             + 2 * 2 * C * d * d                   # (q e^g) S, v' = W S
             + 2 * C * (C + 1) * d                 # A_qk with the diagonal, A_qk v_new
             + 2 * C * d * d + 2 * d * d           # S e^g_last, (k e^(g_last - g))^T v_new
             + 14 * d * C)                         # kb, vb, cumsum g, exps, decays, v - v', sum
    return float(chunk / C)


def mhc_coef_flop(s: Shape) -> int:
    """One mHC site's pre / post / comb epilogue and Sinkhorn on the (2 + N) N projection outputs
    (Glm5NextTextHyperConnection.forward): 1 + 2 (iters - 1) normalisations of the N x N comb."""
    n = s.hc
    pre = n * (1 + 1 + SIGMOID + 1)                # scale, bias, sigmoid, + eps
    post = n * (1 + 1 + SIGMOID + 1)               # scale, bias, sigmoid, x 2
    comb = n * n * (1 + 1 + SOFTMAX + 1)           # scale, bias, softmax, + eps
    return pre + post + comb + (2 * s.sinkhorn - 1) * n * n * 2   # each normalisation: sum (+ eps) and divide


def layer_terms(s: Shape, layer: int) -> list[Term]:
    """Every context-free term of one layer, FLOP per token (§2.1)."""
    H = s.hidden
    t = [Term(m.row, m.stage, m.name, 2 * m.n_out * m.n_in * m.per_token) for m in matmuls(s, layer)]
    for entry, exit_ in (("hc_a", "attn_exit"), ("hc_f", "ffn_exit")):
        t += [Term("mhc", entry, "hc_norm_coef_collapse", s.hc * H * RMSNORM_UNWEIGHTED + mhc_coef_flop(s) + 2 * s.hc * H),
              Term("mhc", exit_, "hc_mix", 2 * s.hc * s.hc * H + 2 * s.hc * H),
              Term("rmsnorm", entry, "rmsnorm", H * RMSNORM)]
    if s.layer_types[layer] == KDA:
        qkv = s.kda_heads * s.kda_dim
        t += [Term("kda_elem", "kda_fb_conv", "conv_silu", 3 * qkv * (2 * s.conv + SILU)),
              Term("kda_elem", "kda_fb_conv", "l2norm_qscale", 2 * qkv * L2NORM + qkv),
              Term("kda_elem", "kda_fb_conv", "forget_gate_beta", qkv * (ADD + 1 + SIGMOID + 1) + s.kda_heads * SIGMOID),
              Term("kda_core", "kda_core", "chunk_core", s.kda_heads * chunk_core_per_head(s)),
              Term("kda_elem", "kda_oproj", "rmsnorm_gated", qkv * (RMSNORM + GATE))]
    else:
        t += [Term("dsa_proj", "dsa_qkv", "q_a_kv_a_rmsnorm", (s.q_lora + s.kv_lora) * RMSNORM),
              Term("idx_proj", "dsa_index", "idx_layernorm_pool", s.idx_dim * LAYERNORM + s.idx_dim * (ADD + SOFTMAX + 2))]
    if s.mlp_types[layer] == "sparse":
        t += [Term("router", "router", "router_select", s.n_experts * (SIGMOID + ADD) + 3 * s.top_k),
              Term("routed", "swiglu_ag", "expert_swiglu", s.top_k * s.moe_inter * SWIGLU),
              Term("routed", "comb_acc", "expert_weight_sum", s.top_k * H * 2),
              Term("shared", "swiglu_ag", "shared_swiglu", s.moe_inter * s.n_shared * SWIGLU),
              Term("shared", "comb_acc", "shared_add", H)]
    else:
        t += [Term("dense_mlp", "swiglu_ag", "dense_swiglu", s.dense_inter * SWIGLU)]
    return t


def pool_score_flop(s: Shape) -> int:
    """§2.2: one (query, complete pool) indexer score: dot products, relu and scale, head-weighted sum."""
    return 2 * s.idx_heads * s.idx_dim + 2 * s.idx_heads + 2 * s.idx_heads


def key_flop(s: Shape) -> int:
    """§2.2: one (query, key) of the top-k attention: QK, PV, softmax over the heads."""
    return 2 * s.dsa_heads * s.qk_head + 2 * s.dsa_heads * s.v_head + SOFTMAX * s.dsa_heads


def sum_pools(s: Shape, a: int, b: int) -> int:
    """sum of floor(n / kpool) for n = a..b: the complete pools each query n (n tokens visible) scores."""
    def upto(N: int) -> int:   # n = 0..N
        q, r = divmod(N + 1, s.kpool)
        return s.kpool * q * (q - 1) // 2 + r * q
    return upto(b) - upto(a - 1)


def sum_keys(s: Shape, a: int, b: int) -> int:
    """sum of keys(n) for n = a..b: n while every pool is selected (n < topk + kpool = 2052),
    else topk + the tail pool's n mod kpool."""
    thr = s.topk + s.kpool

    def mods(N: int) -> int:   # sum of n mod kpool, n = 0..N
        q, r = divmod(N + 1, s.kpool)
        return q * s.kpool * (s.kpool - 1) // 2 + r * (r - 1) // 2
    d1 = min(b, thr - 1)
    dense = (d1 * (d1 + 1) - (a - 1) * a) // 2 if d1 >= a else 0
    s0 = max(a, thr)
    sparse = (b - s0 + 1) * s.topk + mods(b) - mods(s0 - 1) if b >= s0 else 0
    return dense + sparse


def head_flops(s: Shape) -> int:
    """§2.1 head: the last token only, once a request: logits, the 4-stream mean, the final RMSNorm."""
    return 2 * s.hidden * s.vocab + 2 * s.hc * s.hidden + RMSNORM * s.hidden


@dataclass(frozen=True)
class Line:
    layer: int | None   # None for the head
    row: str
    stage: str
    name: str
    flop: float         # over the T tokens


def count(s: Shape, T: int, t_c: int = 0, layers=None) -> list[Line]:
    """FLOP of prefilling T tokens after t_c cached ones (queries see n = t_c + 1 .. t_c + T tokens)."""
    layers = list(range(len(s.layer_types))) if layers is None else list(layers)
    if T < 1 or t_c < 0 or not layers or not set(layers) <= set(range(len(s.layer_types))):
        raise ValueError(f"T {T}, T_c {t_c}, layers {layers}")
    a, b = t_c + 1, t_c + T
    out = []
    for l in layers:
        out += [Line(l, x.row, x.stage, x.name, x.flop * T) for x in layer_terms(s, l)]
        if s.layer_types[l] == DSA:
            out += [Line(l, "idx_score", "dsa_index", "idx_score", pool_score_flop(s) * sum_pools(s, a, b)),
                    Line(l, "dsa_attn", "dsa_attn", "topk_attention", key_flop(s) * sum_keys(s, a, b))]
    if len(s.layer_types) - 1 in layers:
        out.append(Line(None, "head", "head", "head", head_flops(s)))
    return out


def _sum_by(lines: list[Line], key) -> dict[str, float]:
    d: dict[str, float] = {}
    for x in lines:
        d[key(x)] = d.get(key(x), 0.0) + x.flop
    return d


def by_row(lines: list[Line]) -> dict[str, float]:
    d = _sum_by(lines, lambda x: x.row)
    return {k: d[k] for k in ROWS if k in d}


def by_stage(lines: list[Line]) -> dict[str, float]:
    d = _sum_by(lines, lambda x: x.stage)
    return {k: d[k] for k in STAGES if k in d}


def total(lines: list[Line]) -> float:
    return sum(x.flop for x in lines)
