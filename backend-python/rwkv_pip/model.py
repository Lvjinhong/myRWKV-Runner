########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

from typing import Optional
import types, gc, os, time, re
import torch
from torch.nn import functional as F

torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cuda.matmul.allow_tf32 = True
current_path = os.path.dirname(os.path.abspath(__file__))


# https://zhuanlan.zhihu.com/p/612879065
def LoadPreCompileLibrary(file):
    import importlib
    import os

    import torch

    # load the custom_op_library and register the custom ops
    lib_dir = os.path.dirname(__file__)
    if os.name == "nt":
        # Register the main torchvision library location on the default DLL path
        import ctypes
        import sys

        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        with_load_library_flags = hasattr(kernel32, "AddDllDirectory")
        prev_error_mode = kernel32.SetErrorMode(0x0001)

        if with_load_library_flags:
            kernel32.AddDllDirectory.restype = ctypes.c_void_p

        if sys.version_info >= (3, 8):
            os.add_dll_directory(lib_dir)
        elif with_load_library_flags:
            res = kernel32.AddDllDirectory(lib_dir)
            if res is None:
                err = ctypes.WinError(ctypes.get_last_error())
                err.strerror += f' Error adding "{lib_dir}" to the DLL directories.'
                raise ValueError(err)

        kernel32.SetErrorMode(prev_error_mode)

    loader_details = (
        importlib.machinery.ExtensionFileLoader,
        importlib.machinery.EXTENSION_SUFFIXES,
    )

    extfinder = importlib.machinery.FileFinder(lib_dir, loader_details)
    ext_specs = extfinder.find_spec(file)
    if ext_specs is None:
        return False

    try:
        torch.ops.load_library(ext_specs.origin)
    except OSError as exc:
        return False
    return True


########################################################################################################

if os.environ.get("RWKV_JIT_ON") != "0":
    os.environ["RWKV_JIT_ON"] = "1"
    MyModule = torch.jit.ScriptModule
    MyFunction = torch.jit.script_method
    MyStatic = torch.jit.script
else:
    MyModule = torch.nn.Module

    def __nop(ob):
        return ob

    MyFunction = __nop
    MyStatic = __nop

if os.environ.get("RWKV_CUDA_ON") == "1":
    DISABLE_CUBLAS_GEMM = False
    from torch.utils.cpp_extension import load  # L581

    if LoadPreCompileLibrary("wkv_cuda") is False:
        try:
            load(
                name=f"wkv_cuda",
                sources=[
                    f"{current_path}/cuda/wrapper.cpp",
                    f"{current_path}/cuda/operators.cu",
                    f"{current_path}/cuda/gemm_fp16_cublas.cpp",
                ],
                verbose=True,
                extra_ldflags=["cublas.lib" if os.name == "nt" else ""],
                extra_cuda_cflags=[
                    "--use_fast_math",
                    "-O3",
                    "--extra-device-vectorization",
                ],
                is_python_module=False,
            )
            DISABLE_CUBLAS_GEMM = False
        except:
            print(
                "Failed to build cuBLAS matmul, falling back to torch.matmul. Small model with fp16 will overflow."
            )
            load(
                name=f"wkv_cuda",
                sources=[
                    f"{current_path}/cuda/wrapper.cpp",
                    f"{current_path}/cuda/operators.cu",
                ],
                verbose=True,
                extra_cuda_cflags=[
                    "--use_fast_math",
                    "-O3",
                    "--extra-device-vectorization",
                ],
                extra_cflags=["-DDISABLE_CUBLAS_GEMM"],
                is_python_module=False,
            )
            DISABLE_CUBLAS_GEMM = True

    @MyStatic
    def cuda_wkv(T: int, C: int, w, u, k, v, aa, bb, pp):
        assert 1 * C % min(C, 32) == 0
        assert (
            k.dtype == v.dtype == torch.float16 or k.dtype == v.dtype == torch.float32
        )
        assert w.dtype == u.dtype == aa.dtype == bb.dtype == pp.dtype == torch.float32
        w = w.contiguous()
        u = u.contiguous()
        k = k.contiguous()
        v = v.contiguous()
        y = torch.empty(
            (T, C),
            device=w.device,
            memory_format=torch.contiguous_format,
            dtype=k.dtype,
        )
        torch.ops.rwkv.wkv_forward(1, T, C, w, u, k, v, y, aa, bb, pp)
        return y, aa, bb, pp

    @MyStatic
    def cuda_mm8_seq(B: int, N: int, M: int, x, w, mx, rx, my, ry):
        assert x.dtype == mx.dtype == rx.dtype == my.dtype == ry.dtype
        assert x.dtype == torch.float32 or x.dtype == torch.float16
        assert w.dtype == torch.uint8
        assert x.shape == (B, N)
        assert w.shape == (N, M)
        assert rx.shape == mx.shape == (M,)
        assert ry.shape == my.shape == (N, 1)
        y = torch.empty((B, M), device=w.device, dtype=x.dtype)
        torch.ops.rwkv.mm8_seq(B, N, M, x, w, mx, rx, my, ry, y)
        return y

    @MyStatic
    def cuda_mm8_one(N: int, M: int, x, w, mx, rx, my, ry):
        assert x.dtype == mx.dtype == rx.dtype == my.dtype == ry.dtype
        assert x.dtype == torch.float32 or x.dtype == torch.float16
        assert w.dtype == torch.uint8
        assert x.shape == (N,)
        assert w.shape == (N, M)
        assert rx.shape == mx.shape == (M,)
        assert ry.shape == my.shape == (N, 1)
        y = torch.zeros((M,), device=w.device, dtype=torch.float32)
        torch.ops.rwkv.mm8_one(N, M, x, w, mx, rx, my, ry, y)
        return y.to(dtype=x.dtype)

else:
    os.environ["RWKV_CUDA_ON"] = "0"


@MyStatic
def torch_mm8_seq(x, w, mx, rx, my, ry):
    return x @ ((w.to(dtype=x.dtype) + 0.5) * ry * rx + my + mx)


@MyStatic
def torch_mm8_one(x, w, mx, rx, my, ry):
    return x @ ((w.to(dtype=x.dtype) + 0.5) * ry * rx + my + mx)


if os.environ.get("RWKV_CUDA_ON") == "1":

    @MyStatic
    def mm8_seq(x, w, mx, rx, my, ry):
        if w.device.type == "cuda" and x.dtype == torch.float16:
            B, N, M = x.shape[0], w.shape[0], w.shape[1]
            return cuda_mm8_seq(B, N, M, x, w, mx, rx, my, ry)
        else:
            return torch_mm8_seq(x, w, mx, rx, my, ry)

    @MyStatic
    def mm8_one(x, w, mx, rx, my, ry):
        if w.device.type == "cuda":
            N, M = w.shape[0], w.shape[1]
            return cuda_mm8_one(N, M, x, w, mx, rx, my, ry)
        else:
            return torch_mm8_one(x, w, mx, rx, my, ry)

else:

    @MyStatic
    def mm8_seq(x, w, mx, rx, my, ry):
        return torch_mm8_seq(x, w, mx, rx, my, ry)

    @MyStatic
    def mm8_one(x, w, mx, rx, my, ry):
        return torch_mm8_one(x, w, mx, rx, my, ry)


def mm8(
    x: torch.Tensor,
    w: torch.Tensor,
    mx: torch.Tensor,
    rx: torch.Tensor,
    my: torch.Tensor,
    ry: torch.Tensor,
):
    if len(x.shape) == 1:
        return mm8_one(x, w, mx, rx, my, ry)
    return mm8_seq(x, w, mx, rx, my, ry)


def matmul(
    a,
    b,
    mx: Optional[torch.Tensor] = None,
    rx: Optional[torch.Tensor] = None,
    my: Optional[torch.Tensor] = None,
    ry: Optional[torch.Tensor] = None,
    output_dtype: Optional[torch.dtype] = None,
) -> torch.Tensor:
    if output_dtype is None:
        output_dtype = a.dtype
    if b.dtype in [torch.float16, torch.bfloat16, torch.float32]:
        assert a.dtype == b.dtype
        return matmul_float(a, b, output_dtype=output_dtype)
    elif b.dtype == torch.uint8:
        assert mx is not None
        assert rx is not None
        assert my is not None
        assert ry is not None
        return mm8(a, b, mx, rx, my, ry).to(output_dtype)
    else:
        raise ValueError("Unsupported dtype")


if os.environ.get("RWKV_CUDA_ON") == "1" and not DISABLE_CUBLAS_GEMM:

    def matmul_float(a, b, output_dtype: Optional[torch.dtype] = None):
        if output_dtype is None:
            output_dtype = a.dtype
        if a.dtype == b.dtype == torch.float16 and a.device.type == "cuda":
            if len(a.shape) == 1:
                assert len(b.shape) == 2
                c = torch.empty((b.shape[-1],), dtype=output_dtype, device=a.device)
                a = a.unsqueeze(0)
            else:
                assert len(a.shape) == len(b.shape)
                assert len(a.shape) == 2 or len(a.shape) == 3
                # torch.empty((*a.shape[:-1], b.shape[-1])) doesn't work with jit
                if len(a.shape) == 2:
                    c = torch.empty(
                        (a.shape[0], b.shape[-1]), dtype=output_dtype, device=a.device
                    )
                else:
                    c = torch.empty(
                        (a.shape[0], a.shape[1], b.shape[-1]),
                        dtype=output_dtype,
                        device=a.device,
                    )
            torch.ops.rwkv.gemm_fp16_cublas(a, b, c)
            return c
        else:
            return (a @ b).to(output_dtype)

else:

    def matmul_float(a, b, output_dtype: Optional[torch.dtype] = None):
        return (a @ b).to(output_dtype)


########################################################################################################


class RWKV(MyModule):
    def __init__(self, model, strategy, verbose=True, convert_and_save_and_exit=None):
        super().__init__()
        if verbose:
            prxxx = lambda *args, **kwargs: print(*args, **kwargs)
        else:
            prxxx = lambda *args, **kwargs: None

        STRATEGY_REGEX = r"^(?:(?:^|->) *(?:cuda(?::[\d]+)?|cpu|mps|dml) (?:fp(?:16|32)|bf16)(?:i8|i4|i3)?(?: \*[\d]+\+?)? *)+$"
        if not re.match(STRATEGY_REGEX, strategy):
            raise ValueError(
                "Invalid strategy. Please read https://pypi.org/project/rwkv/"
            )

        strategy = ("->".join([x.strip() for x in strategy.split("->")])).replace(
            "->", " -> "
        )
        self.args = types.SimpleNamespace()
        args = self.args
        args.MODEL_NAME = model
        args.strategy_string = strategy

        # Rescale for fp16 mode: set x = x/2 every X layer (to avoid fp16 overflow)
        try:
            self.RESCALE_LAYER = int(
                os.environ["RWKV_RESCALE_LAYER"]
            )  # !!! NOTE: SEEMS YOU SHOULD SET IT TO 999 (disable) FOR RWKV-MUSIC MODELS !!!
        except:
            self.RESCALE_LAYER = 6 if "fp16" in strategy else 0
        prxxx(
            f'RWKV_JIT_ON {os.environ["RWKV_JIT_ON"]} RWKV_CUDA_ON {os.environ["RWKV_CUDA_ON"]} RESCALE_LAYER {self.RESCALE_LAYER}\n'
        )

        args.MODEL_NAME = args.MODEL_NAME.strip()
        if not args.MODEL_NAME.endswith(".pth"):
            args.MODEL_NAME += ".pth"
        prxxx(f"Loading {args.MODEL_NAME} ...")
        with torch.no_grad():
            self.w = torch.load(
                args.MODEL_NAME, map_location="cpu"
            )  # load model to CPU first
            gc.collect()
            w = self.w

            ALREADY_CONVERTED = False
            if "_strategy" in w:
                ALREADY_CONVERTED = True
                assert (
                    convert_and_save_and_exit == None
                )  # you should only convert a raw model
                prxxx(
                    f"Converted model: strategy {w['_strategy']}, version {w['_version']}\n"
                )
                assert (
                    w["_strategy"] == args.strategy_string
                )  # if you are using a new strategy, re-convert the model
                assert (
                    float(w["_version"]) >= 0.7
                )  # sometimes you should re-convert using latest convert_model.py
                assert (
                    w["_rescale_layer"] == self.RESCALE_LAYER
                )  # must use same RESCALE_LAYER to avoid mistakes
                del w["_strategy"]
                del w["_version"]
                del w["_rescale_layer"]

            args.n_embd = w["emb.weight"].shape[1]
            args.n_att = w["blocks.0.att.key.weight"].shape[
                0
            ]  # note: transposed matrix
            args.n_ffn = w["blocks.0.ffn.key.weight"].shape[
                0
            ]  # note: transposed matrix
            args.n_layer = 0
            keys = list(w.keys())
            self.version = 4
            for x in keys:
                layer_id = int(x.split(".")[1]) if ("blocks." in x) else 0
                args.n_layer = max(args.n_layer, layer_id + 1)
                if "ln_x" in x:
                    self.version = max(5, self.version)
                if "gate.weight" in x:
                    self.version = max(5.1, self.version)
                if int(self.version) == 5 and "att.time_decay" in x:
                    args.n_head = w[x].shape[0]
                    if len(w[x].shape) > 1:
                        if w[x].shape[1] > 1:
                            self.version = max(5.2, self.version)

            ####################### Compute strategy

            s = [x.strip().split(" ") for x in strategy.split("->")]
            plan = [0] * len(s)
            stream_i = -1
            stream_count = 0
            to_allocate = args.n_layer + 1
            allocated = 0
            free_slots = 0
            for i in range(len(s)):
                si = s[i]
                si1 = si[1]
                if si1.startswith("fp32"):
                    si[1] = [torch.float]
                elif si1.startswith("fp16"):
                    si[1] = [torch.float16]
                elif si1.startswith("bf16"):
                    si[1] = [torch.bfloat16]
                if si1.endswith("i8"):
                    si[1] += [torch.uint8]
                else:
                    si[1] += [si[1][0]]
                if len(si) > 2:
                    ss = si[2]
                    assert ss.startswith("*")
                    if ss.endswith("+"):
                        plan[i] = int(ss[1:-1])
                        stream_i = i
                    else:
                        plan[i] = int(ss[1:])
                    allocated += plan[i]
                    if allocated >= to_allocate:
                        plan[i] += to_allocate - allocated
                        break
                else:
                    free_slots += 1
            if stream_i < 0:
                if free_slots > 0 and to_allocate > allocated:
                    for i in range(len(s)):
                        if plan[i] == 0:
                            plan[i] = (to_allocate - allocated) // free_slots
                            allocated += plan[i]
                            free_slots -= 1
                if to_allocate > allocated:
                    plan[len(s) - 1] += to_allocate - allocated
            else:
                if to_allocate > allocated:
                    stream_count = to_allocate - allocated
                    plan[stream_i] += stream_count
            prxxx(f"Strategy: (total {args.n_layer}+1={args.n_layer+1} layers)")
            for i in range(len(s)):
                ss = s[i]
                if i != stream_i:
                    prxxx(
                        f'* {ss[0]} {str(ss[1]).replace("torch.","")}, store {plan[i]} layers'
                    )
                else:
                    prxxx(
                        f'* {ss[0]} {str(ss[1]).replace("torch.","")}, store {plan[i]-stream_count} layers, stream {stream_count} layers'
                    )
                plan[i] += 0 if i == 0 else plan[i - 1]
            self.strategy = [None] * (args.n_layer + 1)
            strategy = self.strategy
            for n in range(args.n_layer + 1):
                for i in range(len(s)):
                    if n < plan[i]:
                        strategy[n] = types.SimpleNamespace()
                        strategy[n].device = s[i][0]
                        strategy[n].atype = s[i][1][0]
                        strategy[n].wtype = s[i][1][1]
                        strategy[n].stream = False
                        if strategy[n].device == "dml":
                            import torch_directml

                            strategy[n].device = torch_directml.device()
                        if i == stream_i and n >= (plan[i] - stream_count):
                            strategy[n].stream = True
                        break
                prxxx(
                    f"{n}-{strategy[n].device}-{str(strategy[n].atype).replace('torch.','')}-{str(strategy[n].wtype).replace('torch.','')}{'-stream' if strategy[n].stream else ''}",
                    end=" ",
                )
            prxxx()

            ####################### Load weights to self.w

            if not ALREADY_CONVERTED:
                try:  # precompute embedding
                    w["emb.weight"] = F.layer_norm(
                        w["emb.weight"],
                        (args.n_embd,),
                        weight=w["blocks.0.ln0.weight"],
                        bias=w["blocks.0.ln0.bias"],
                    )
                except:
                    w["emb.weight"] = F.layer_norm(
                        w["emb.weight"].float(),
                        (args.n_embd,),
                        weight=w["blocks.0.ln0.weight"].float(),
                        bias=w["blocks.0.ln0.bias"].float(),
                    )
                del w["blocks.0.ln0.weight"]
                del w["blocks.0.ln0.bias"]

            print_need_newline = False

            REAL_TIME_FIRST = False
            for x in list(w.keys()):
                if ".time_faaaa" in x:
                    REAL_TIME_FIRST = True
            if REAL_TIME_FIRST:
                w = {
                    k.replace(".time_faaaa", ".time_first")
                    if ".time_faaaa" in k
                    else k: v
                    for k, v in w.items()
                }
                self.w = w

            keys = list(w.keys())
            for x in keys:
                w[x].requires_grad = False
                layer_id = int(x.split(".")[1]) if ("blocks." in x) else 0
                if ("ln_out." in x) or ("head." in x):
                    layer_id = args.n_layer
                dd = strategy[layer_id]
                DEVICE = dd.device
                ATYPE = dd.atype
                WTYPE = dd.wtype

                if not ALREADY_CONVERTED:
                    if self.RESCALE_LAYER > 0:
                        if "att.output.weight" in x:
                            w[x] = w[x] / (2 ** int(layer_id // self.RESCALE_LAYER))
                        if "ffn.value.weight" in x:
                            w[x] = w[x] / (2 ** int(layer_id // self.RESCALE_LAYER))

                    if ".time_" in x:
                        w[x] = w[x].squeeze()
                    if (
                        "key.weight" in x
                        or "value.weight" in x
                        or "receptance.weight" in x
                        or "gate.weight" in x
                        or "output.weight" in x
                        or "head.weight" in x
                    ):
                        w[x] = w[x].t()

                    if ".time_decay" in x:  # need fp32 for this
                        if self.version == 4:
                            w[x] = -torch.exp(w[x].float())
                        elif int(self.version) == 5:
                            w[x] = torch.exp(-torch.exp(w[x].float())).reshape(-1, 1, 1)
                            if self.version == 5.2:
                                w[x] = w[x].reshape(args.n_head, -1, 1)
                    elif ".time_first" in x:  # need fp32 for this
                        if self.version == 4:
                            w[x] = w[x].float()
                        elif int(self.version) == 5:
                            if REAL_TIME_FIRST:
                                w[x] = w[x].float().reshape(-1, 1, 1)
                            else:
                                w[x] = torch.exp(w[x].float()).reshape(-1, 1, 1)
                            if self.version == 5.2:
                                w[x] = w[x].reshape(args.n_head, -1, 1)
                    elif ".ln_x" in x:  # need fp32 for group_norm
                        w[x] = w[x].float()
                    else:
                        if (len(w[x].shape) == 2) and ("emb" not in x):
                            if WTYPE != torch.uint8:
                                w[x] = w[x].to(dtype=WTYPE)
                            else:
                                w[x] = w[x].float()

                                if w[x].shape[0] > w[x].shape[1]:
                                    w[x + "_my"] = torch.amin(w[x], dim=1).unsqueeze(1)
                                    w[x] = w[x] - w[x + "_my"]
                                    w[x + "_mx"] = torch.amin(w[x], dim=0)
                                    w[x] = w[x] - w[x + "_mx"]
                                    w[x + "_rx"] = torch.amax(w[x], dim=0)
                                    w[x] = w[x] / w[x + "_rx"]
                                    w[x + "_ry"] = torch.amax(w[x], dim=1).unsqueeze(1)
                                    w[x] = w[x] / w[x + "_ry"]
                                else:
                                    w[x + "_mx"] = torch.amin(w[x], dim=0)
                                    w[x] = w[x] - w[x + "_mx"]
                                    w[x + "_my"] = torch.amin(w[x], dim=1).unsqueeze(1)
                                    w[x] = w[x] - w[x + "_my"]
                                    w[x + "_rx"] = torch.amax(w[x], dim=0)
                                    w[x] = w[x] / w[x + "_rx"]
                                    w[x + "_ry"] = torch.amax(w[x], dim=1).unsqueeze(1)
                                    w[x] = w[x] / w[x + "_ry"]

                                w[x] = torch.clip(
                                    torch.floor(w[x] * 256), min=0, max=255
                                ).to(dtype=torch.uint8)
                                w[x + "_mx"] = w[x + "_mx"].to(dtype=ATYPE).contiguous()
                                w[x + "_rx"] = (
                                    (w[x + "_rx"] / 16).to(dtype=ATYPE).contiguous()
                                )
                                w[x + "_my"] = w[x + "_my"].to(dtype=ATYPE).contiguous()
                                w[x + "_ry"] = (
                                    (w[x + "_ry"] / 16).to(dtype=ATYPE).contiguous()
                                )
                        else:
                            w[x] = w[x].to(dtype=ATYPE)

                if convert_and_save_and_exit == None:
                    if "emb." in x:
                        w[x] = w[x].contiguous()
                    elif (dd.stream) and (
                        x.endswith("key.weight")
                        or x.endswith("value.weight")
                        or x.endswith("receptance.weight")
                        or x.endswith("output.weight")
                    ):
                        try:
                            w[x] = (
                                w[x].contiguous().pin_memory()
                            )  # if you see "CUDA error: out of memory" here, that's out of CPU RAM, not VRAM. Get more RAM :)
                        except:
                            print(
                                "Note: You are running out of RAM. Get more CPU RAM. Now this will run much slower."
                            )
                    elif DEVICE != "cpu":
                        w[x] = w[x].to(device=DEVICE).contiguous()

                    if (dd.stream) or (DEVICE != "cpu"):
                        try:
                            w[x + "_mx"] = w[x + "_mx"].to(device=DEVICE).contiguous()
                            w[x + "_rx"] = w[x + "_rx"].to(device=DEVICE).contiguous()
                            w[x + "_my"] = w[x + "_my"].to(device=DEVICE).contiguous()
                            w[x + "_ry"] = w[x + "_ry"].to(device=DEVICE).contiguous()
                        except:
                            pass

                if "ffn.value.weight" in x:
                    gc.collect()
                    if "cuda" in args.strategy_string:
                        torch.cuda.empty_cache()

                shape = [i for i in w[x].shape if i != 1]
                if len(shape) > 1:
                    shape = f" {str(shape[0]).rjust(5)} {str(shape[1]).rjust(5)}"
                else:
                    shape = f" {str(shape[0]).rjust(5)}      "
                if layer_id == 0 or layer_id >= args.n_layer - 1:
                    if print_need_newline:
                        prxxx("\n", end="")
                        print_need_newline = False
                    dt = str(w[x].dtype).replace("torch.", "")
                    dt = (
                        dt.replace("float32", "f32")
                        .replace("bfloat16", "bf16")
                        .replace("float16", "f16")
                        .replace("uint8", "i8")
                    )
                    prxxx(
                        x.ljust(32),
                        dt.rjust(4),
                        str(w[x].device).rjust(8),
                        shape,
                        " (pinned)" if w[x].is_pinned() else "",
                    )
                else:
                    print_need_newline = True
                    prxxx(".", end="", flush=True)

            if convert_and_save_and_exit:
                w["_strategy"] = args.strategy_string
                w["_rescale_layer"] = self.RESCALE_LAYER
                w["_version"] = "0.7"
                if not convert_and_save_and_exit.endswith(".pth"):
                    convert_and_save_and_exit += ".pth"
                prxxx(f"Saving to {convert_and_save_and_exit}...")
                torch.save(w, convert_and_save_and_exit)
                prxxx(f"Converted and saved. Now this will exit.")
                exit(0)

            if self.version == 5.2 and os.environ["RWKV_CUDA_ON"] == "1":
                HEAD_SIZE = args.n_att // args.n_head
                if LoadPreCompileLibrary("rwkv5") is True:
                    rwkv5 = torch.ops.rwkv5
                else:
                    rwkv5 = load(
                        name="rwkv5",
                        sources=[
                            f"{current_path}/cuda/rwkv5_op.cpp",
                            f"{current_path}/cuda/rwkv5.cu",
                        ],
                        verbose=True,
                        extra_cuda_cflags=[
                            "-res-usage",
                            "--use_fast_math",
                            "-O3",
                            "-Xptxas -O3" if os.name != "nt" else "",
                            "--extra-device-vectorization",
                            f"-D_N_={HEAD_SIZE}",
                        ],
                    )

                class RWKV_5(torch.autograd.Function):
                    @staticmethod
                    def forward(ctx, B, T, C, H, state, r, k, v, w, u):
                        with torch.no_grad():
                            assert HEAD_SIZE == C // H
                            ctx.B = B
                            ctx.T = T
                            ctx.C = C
                            ctx.H = H
                            assert state.dtype == torch.float32
                            assert w.dtype == torch.float32
                            assert r.is_contiguous()
                            assert k.is_contiguous()
                            assert v.is_contiguous()
                            assert w.is_contiguous()
                            assert u.is_contiguous()
                            assert state.is_contiguous()

                        y = torch.empty(
                            (B, T, C),
                            device=w.device,
                            dtype=r.dtype,
                            memory_format=torch.contiguous_format,
                        )
                        if r.dtype == torch.bfloat16:
                            rwkv5.forward_bf16(B, T, C, H, state, r, k, v, w, u, y)
                        elif r.dtype == torch.float16:
                            rwkv5.forward_fp16(B, T, C, H, state, r, k, v, w, u, y)
                        elif r.dtype == torch.float32:
                            rwkv5.forward_fp32(B, T, C, H, state, r, k, v, w, u, y)
                        return y, state

                self.RWKV_5 = RWKV_5

            gc.collect()
            if "cuda" in args.strategy_string:
                torch.cuda.empty_cache()

    def RUN_RWKV_5(self, B, T, C, H, state, r, k, v, w, u):
        return self.RWKV_5.apply(B, T, C, H, state, r, k, v, w, u)

    ########################################################################################################

    @MyFunction
    def ffn_one(
        self,
        x,
        sx,
        ln_w,
        ln_b,
        k_mix,
        r_mix,
        kw,
        vw,
        rw,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(matmul(rx, rw, rmx, rrx, rmy, rry))
        vx = torch.square(torch.relu(matmul(kx, kw, kmx, krx, kmy, kry)))
        out = r * matmul(vx, vw, vmx, vrx, vmy, vry)
        return x + out, xx

    ########################################################################################################

    @MyFunction
    def ffn_seq(
        self,
        x,
        sx,
        ln_w,
        ln_b,
        k_mix,
        r_mix,
        kw,
        vw,
        rw,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
        kx = xx * k_mix + sx * (1 - k_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(matmul(rx, rw, rmx, rrx, rmy, rry))
        vx = torch.square(torch.relu(matmul(kx, kw, kmx, krx, kmy, kry)))
        out = r * matmul(vx, vw, vmx, vrx, vmy, vry)
        return x + out, xx[-1, :]

    ########################################################################################################

    @MyFunction
    def att_one(
        self,
        x,
        sx,
        aa,
        bb,
        pp,
        ln_w,
        ln_b,
        k_mix,
        v_mix,
        r_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(matmul(rx, rw, rmx, rrx, rmy, rry))
        k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
        v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)

        ww = t_first + k
        p = torch.maximum(pp, ww)
        e1 = torch.exp(pp - p)
        e2 = torch.exp(ww - p)
        wkv = ((e1 * aa + e2 * v) / (e1 * bb + e2)).to(dtype=x.dtype)
        ww = t_decay + pp
        p = torch.maximum(ww, k)
        e1 = torch.exp(ww - p)
        e2 = torch.exp(k - p)

        out = matmul(r * wkv, ow, omx, orx, omy, ory)
        return x + out, xx, e1 * aa + e2 * v, e1 * bb + e2, p

    ########################################################################################################

    @MyFunction
    def att_seq(
        self,
        x,
        sx,
        aa,
        bb,
        pp,
        ln_w,
        ln_b,
        k_mix,
        v_mix,
        r_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        r = torch.sigmoid(matmul(rx, rw, rmx, rrx, rmy, rry))
        k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
        v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)

        T = x.shape[0]
        for t in range(T):
            kk = k[t]
            vv = v[t]
            ww = t_first + kk
            p = torch.maximum(pp, ww)
            e1 = torch.exp(pp - p)
            e2 = torch.exp(ww - p)
            sx[t] = ((e1 * aa + e2 * vv) / (e1 * bb + e2)).to(dtype=x.dtype)
            ww = t_decay + pp
            p = torch.maximum(ww, kk)
            e1 = torch.exp(ww - p)
            e2 = torch.exp(kk - p)
            aa = e1 * aa + e2 * vv
            bb = e1 * bb + e2
            pp = p
        out = matmul(r * sx, ow, omx, orx, omy, ory)
        return x + out, xx[-1, :], aa, bb, pp

    ########################################################################################################

    @MyFunction
    def att_one_v5(
        self,
        x,
        sx,
        s,
        ln_w,
        ln_b,
        lx_w,
        lx_b,
        k_mix,
        v_mix,
        r_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        H = t_decay.shape[0]
        S = x.shape[-1] // H

        r = matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32).view(H, 1, S)
        k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32).view(H, S, 1)
        v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32).view(H, 1, S)

        a = matmul(k, v)
        out = r @ (t_first * a + s)
        s = a + t_decay * s

        out = out.flatten()
        out = F.group_norm(
            out.unsqueeze(0), num_groups=H, weight=lx_w, bias=lx_b
        ).squeeze(0)
        out = out.to(dtype=x.dtype)
        out = matmul(out, ow, omx, orx, omy, ory)

        return x + out, xx, s

    @MyFunction
    def att_seq_v5(
        self,
        x,
        sx,
        s,
        ln_w,
        ln_b,
        lx_w,
        lx_b,
        k_mix,
        v_mix,
        r_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)

        H = t_decay.shape[0]
        S = x.shape[-1] // H
        T = x.shape[0]

        w = t_decay.reshape(-1, 1)
        u = t_first.reshape(-1, 1)
        ws = w.pow(T).reshape(H, 1, 1)
        ind = torch.arange(T - 1, -1, -1, device=w.device).unsqueeze(0).repeat(H, 1)
        w = w.repeat(1, T).pow(ind)
        wk = w.reshape(H, 1, T)
        wb = wk.transpose(-2, -1).flip(1)
        w = torch.cat([w[:, 1:], u], dim=1)
        w = F.pad(w, (0, T))
        w = torch.tile(w, [T])
        w = w[:, :-T].reshape(-1, T, 2 * T - 1)
        w = w[:, :, T - 1 :].reshape(H, T, T)

        r = (
            matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )
        k = (
            matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
            .transpose(-2, -1)
        )
        v = (
            matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )

        out = ((r @ k) * w) @ v + (r @ s) * wb
        s = ws * s + (k * wk) @ v

        out = out.transpose(0, 1).contiguous().reshape(T, H * S)
        out = F.group_norm(out, num_groups=H, weight=lx_w, bias=lx_b)
        out = out.to(dtype=x.dtype)
        out = matmul(out, ow, omx, orx, omy, ory)

        return x + out, xx[-1, :], s

    ########################################################################################################

    @MyFunction
    def att_one_v5_1(
        self,
        x,
        sx,
        s,
        ln_w,
        ln_b,
        lx_w,
        lx_b,
        k_mix,
        v_mix,
        r_mix,
        g_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        gw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        gmx,
        grx,
        gmy,
        gry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)
        gx = xx * g_mix + sx * (1 - g_mix)

        H = t_decay.shape[0]
        S = x.shape[-1] // H

        r = matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32).view(H, 1, S)
        k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32).view(H, S, 1)
        v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32).view(H, 1, S)
        g = F.silu(matmul(gx, gw, gmx, grx, gmy, gry))

        a = matmul(k, v)
        out = r @ (t_first * a + s)
        s = a + t_decay * s

        out = out.flatten()
        out = F.group_norm(
            out.unsqueeze(0), num_groups=H, weight=lx_w, bias=lx_b
        ).squeeze(0)
        out = out.to(dtype=x.dtype) * g
        out = matmul(out, ow, omx, orx, omy, ory)

        return x + out, xx, s

    @MyFunction
    def att_seq_v5_1(
        self,
        x,
        sx,
        s,
        ln_w,
        ln_b,
        lx_w,
        lx_b,
        k_mix,
        v_mix,
        r_mix,
        g_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        gw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        gmx,
        grx,
        gmy,
        gry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)
        gx = xx * g_mix + sx * (1 - g_mix)

        H = t_decay.shape[0]
        S = x.shape[-1] // H
        T = x.shape[0]

        w = t_decay.reshape(-1, 1)
        u = t_first.reshape(-1, 1)
        ws = w.pow(T).reshape(H, 1, 1)
        ind = torch.arange(T - 1, -1, -1, device=w.device).unsqueeze(0).repeat(H, 1)
        w = w.repeat(1, T).pow(ind)
        wk = w.reshape(H, 1, T)
        wb = wk.transpose(-2, -1).flip(1)
        w = torch.cat([w[:, 1:], u], dim=1)
        w = F.pad(w, (0, T))
        w = torch.tile(w, [T])
        w = w[:, :-T].reshape(-1, T, 2 * T - 1)
        w = w[:, :, T - 1 :].reshape(H, T, T)

        r = (
            matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )
        k = (
            matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
            .transpose(-2, -1)
        )
        v = (
            matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )
        g = F.silu(matmul(gx, gw, gmx, grx, gmy, gry))

        out = ((r @ k) * w) @ v + (r @ s) * wb
        s = ws * s + (k * wk) @ v

        out = out.transpose(0, 1).contiguous().reshape(T, H * S)
        out = F.group_norm(out, num_groups=H, weight=lx_w, bias=lx_b)
        out = out.to(dtype=x.dtype) * g
        out = matmul(out, ow, omx, orx, omy, ory)

        return x + out, xx[-1, :], s

    ########################################################################################################

    @MyFunction
    def att_seq_v5_2(
        self,
        x,
        sx,
        s,
        ln_w,
        ln_b,
        lx_w,
        lx_b,
        k_mix,
        v_mix,
        r_mix,
        g_mix,
        t_decay,
        t_first,
        kw,
        vw,
        rw,
        gw,
        ow,
        kmx,
        krx,
        kmy,
        kry,
        vmx,
        vrx,
        vmy,
        vry,
        rmx,
        rrx,
        rmy,
        rry,
        gmx,
        grx,
        gmy,
        gry,
        omx,
        orx,
        omy,
        ory,
    ):
        xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
        sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
        kx = xx * k_mix + sx * (1 - k_mix)
        vx = xx * v_mix + sx * (1 - v_mix)
        rx = xx * r_mix + sx * (1 - r_mix)
        gx = xx * g_mix + sx * (1 - g_mix)

        H = t_decay.shape[0]
        S = x.shape[-1] // H
        T = x.shape[0]

        r = (
            matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )
        k = (
            matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
            .transpose(-2, -1)
        )
        v = (
            matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)
            .view(T, H, S)
            .transpose(0, 1)
        )
        g = F.silu(matmul(gx, gw, gmx, grx, gmy, gry))

        out = torch.empty((T, H, S), dtype=r.dtype, device=r.device)
        for t in range(T):
            rt = r[:, t : t + 1, :]
            kt = k[:, :, t : t + 1]
            vt = v[:, t : t + 1, :]
            at = matmul(kt, vt)
            out[t] = (rt @ (t_first * at + s)).squeeze(1)
            s = at + t_decay * s

        out = out.reshape(T, H * S)
        out = F.group_norm(out, num_groups=H, weight=lx_w, bias=lx_b)
        out = out.to(dtype=x.dtype) * g
        out = matmul(out, ow, omx, orx, omy, ory)

        return x + out, xx[-1, :], s

    ########################################################################################################

    if os.environ["RWKV_CUDA_ON"] == "1":

        @MyFunction
        def cuda_att_seq(
            self,
            x,
            sx,
            aa,
            bb,
            pp,
            ln_w,
            ln_b,
            k_mix,
            v_mix,
            r_mix,
            t_decay,
            t_first,
            kw,
            vw,
            rw,
            ow,
            kmx,
            krx,
            kmy,
            kry,
            vmx,
            vrx,
            vmy,
            vry,
            rmx,
            rrx,
            rmy,
            rry,
            omx,
            orx,
            omy,
            ory,
        ):
            T, C = x.shape
            xx = F.layer_norm(x, (C,), weight=ln_w, bias=ln_b)
            sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
            kx = xx * k_mix + sx * (1 - k_mix)
            vx = xx * v_mix + sx * (1 - v_mix)
            rx = xx * r_mix + sx * (1 - r_mix)

            r = torch.sigmoid(matmul(rx, rw, rmx, rrx, rmy, rry))
            k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
            v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)
            y, aa, bb, pp = cuda_wkv(T, C, t_decay, t_first, k, v, aa, bb, pp)

            out = matmul(r * y.to(x.dtype), ow, omx, orx, omy, ory)
            return x + out, xx[-1, :], aa, bb, pp

        # NOTE: decorate with @MyFunction causes JIT error
        def cuda_att_seq_v5_2(
            self,
            x,
            sx,
            s,
            ln_w,
            ln_b,
            lx_w,
            lx_b,
            k_mix,
            v_mix,
            r_mix,
            g_mix,
            t_decay,
            t_first,
            kw,
            vw,
            rw,
            gw,
            ow,
            kmx,
            krx,
            kmy,
            kry,
            vmx,
            vrx,
            vmy,
            vry,
            rmx,
            rrx,
            rmy,
            rry,
            gmx,
            grx,
            gmy,
            gry,
            omx,
            orx,
            omy,
            ory,
        ):
            xx = F.layer_norm(x, (x.shape[-1],), weight=ln_w, bias=ln_b)
            sx = torch.cat((sx.unsqueeze(0), xx[:-1, :]))
            kx = xx * k_mix + sx * (1 - k_mix)
            vx = xx * v_mix + sx * (1 - v_mix)
            rx = xx * r_mix + sx * (1 - r_mix)
            gx = xx * g_mix + sx * (1 - g_mix)

            H = t_decay.shape[0]
            N = x.shape[-1] // H
            T = x.shape[0]

            r = matmul(rx, rw, rmx, rrx, rmy, rry, output_dtype=torch.float32)
            k = matmul(kx, kw, kmx, krx, kmy, kry, output_dtype=torch.float32)
            v = matmul(vx, vw, vmx, vrx, vmy, vry, output_dtype=torch.float32)
            g = F.silu(matmul(gx, gw, gmx, grx, gmy, gry))

            out, s = self.RUN_RWKV_5(
                1,
                T,
                self.args.n_att,
                H,
                s.transpose(-1, -2).contiguous(),
                r,
                k,
                v,
                w=t_decay,
                u=t_first,
            )
            s = s.transpose(-1, -2)

            out = out.reshape(T, H * N)
            out = F.group_norm(out, num_groups=H, weight=lx_w, bias=lx_b)
            out = out.to(dtype=x.dtype) * g
            out = matmul(out, ow, omx, orx, omy, ory)

            return x + out, xx[-1, :], s

    ########################################################################################################

    def forward(self, tokens, state, full_output=False):
        with torch.no_grad():
            w = self.w
            args = self.args

            if state == None:
                if self.version == 4:
                    state = [None] * args.n_layer * 5
                    for i in range(
                        args.n_layer
                    ):  # state: 0=att_xx 1=att_aa 2=att_bb 3=att_pp 4=ffn_xx
                        dd = self.strategy[i]
                        dev = dd.device
                        atype = dd.atype
                        state[i * 5 + 0] = torch.zeros(
                            args.n_embd, dtype=atype, requires_grad=False, device=dev
                        ).contiguous()
                        state[i * 5 + 1] = torch.zeros(
                            args.n_att,
                            dtype=torch.float,
                            requires_grad=False,
                            device=dev,
                        ).contiguous()
                        state[i * 5 + 2] = torch.zeros(
                            args.n_att,
                            dtype=torch.float,
                            requires_grad=False,
                            device=dev,
                        ).contiguous()
                        state[i * 5 + 3] = (
                            torch.zeros(
                                args.n_att,
                                dtype=torch.float,
                                requires_grad=False,
                                device=dev,
                            ).contiguous()
                            - 1e30
                        )
                        state[i * 5 + 4] = torch.zeros(
                            args.n_embd, dtype=atype, requires_grad=False, device=dev
                        ).contiguous()
                elif int(self.version) == 5:
                    state = [None] * args.n_layer * 3
                    for i in range(args.n_layer):  # state: 0=att_xx 1=att_kv 2=ffn_xx
                        dd = self.strategy[i]
                        dev = dd.device
                        atype = dd.atype
                        state[i * 3 + 0] = torch.zeros(
                            args.n_embd, dtype=atype, requires_grad=False, device=dev
                        ).contiguous()
                        state[i * 3 + 1] = torch.zeros(
                            (
                                args.n_head,
                                args.n_att // args.n_head,
                                args.n_att // args.n_head,
                            ),
                            dtype=torch.float,
                            requires_grad=False,
                            device=dev,
                        ).contiguous()
                        state[i * 3 + 2] = torch.zeros(
                            args.n_embd, dtype=atype, requires_grad=False, device=dev
                        ).contiguous()

            seq_mode = len(tokens) > 1

            x = w["emb.weight"][tokens if seq_mode else tokens[0]]

            for i in range(args.n_layer):
                bbb = f"blocks.{i}."
                att = f"blocks.{i}.att."
                ffn = f"blocks.{i}.ffn."
                dd = self.strategy[i]
                dev = dd.device
                atype = dd.atype
                wtype = dd.wtype
                if seq_mode:
                    cuda_applicable = os.environ[
                        "RWKV_CUDA_ON"
                    ] == "1" and "cuda" in str(dev)
                    if cuda_applicable:
                        ATT = self.cuda_att_seq
                    else:
                        ATT = self.att_seq
                    if self.version == 5:
                        ATT = self.att_seq_v5
                    elif self.version == 5.1:
                        ATT = self.att_seq_v5_1
                    elif self.version == 5.2:
                        ATT = self.att_seq_v5_2
                        if cuda_applicable:
                            ATT = self.cuda_att_seq_v5_2
                    FFN = self.ffn_seq
                else:
                    ATT = self.att_one
                    if self.version == 5:
                        ATT = self.att_one_v5
                    elif self.version == 5.1:
                        ATT = self.att_one_v5_1
                    elif self.version == 5.2:
                        ATT = self.att_one_v5_1  # same as v5.1
                    FFN = self.ffn_one

                x = x.to(dtype=atype, device=dev)

                kw = w[f"{att}key.weight"]
                vw = w[f"{att}value.weight"]
                rw = w[f"{att}receptance.weight"]
                ow = w[f"{att}output.weight"]
                if dd.stream:
                    kw = kw.to(device=dev, non_blocking=True)
                    vw = vw.to(device=dev, non_blocking=True)
                    rw = rw.to(device=dev, non_blocking=True)
                    ow = ow.to(device=dev, non_blocking=True)
                kmx = w[f"{att}key.weight_mx"] if wtype == torch.uint8 else x
                krx = w[f"{att}key.weight_rx"] if wtype == torch.uint8 else x
                kmy = w[f"{att}key.weight_my"] if wtype == torch.uint8 else x
                kry = w[f"{att}key.weight_ry"] if wtype == torch.uint8 else x
                vmx = w[f"{att}value.weight_mx"] if wtype == torch.uint8 else x
                vrx = w[f"{att}value.weight_rx"] if wtype == torch.uint8 else x
                vmy = w[f"{att}value.weight_my"] if wtype == torch.uint8 else x
                vry = w[f"{att}value.weight_ry"] if wtype == torch.uint8 else x
                rmx = w[f"{att}receptance.weight_mx"] if wtype == torch.uint8 else x
                rrx = w[f"{att}receptance.weight_rx"] if wtype == torch.uint8 else x
                rmy = w[f"{att}receptance.weight_my"] if wtype == torch.uint8 else x
                rry = w[f"{att}receptance.weight_ry"] if wtype == torch.uint8 else x
                omx = w[f"{att}output.weight_mx"] if wtype == torch.uint8 else x
                orx = w[f"{att}output.weight_rx"] if wtype == torch.uint8 else x
                omy = w[f"{att}output.weight_my"] if wtype == torch.uint8 else x
                ory = w[f"{att}output.weight_ry"] if wtype == torch.uint8 else x
                if self.version == 5.1 or self.version == 5.2:
                    gw = w[f"{att}gate.weight"]
                    if dd.stream:
                        gw = gw.to(device=dev, non_blocking=True)
                    gmx = w[f"{att}gate.weight_mx"] if wtype == torch.uint8 else x
                    grx = w[f"{att}gate.weight_rx"] if wtype == torch.uint8 else x
                    gmy = w[f"{att}gate.weight_my"] if wtype == torch.uint8 else x
                    gry = w[f"{att}gate.weight_ry"] if wtype == torch.uint8 else x
                if self.version == 4:
                    (
                        x,
                        state[i * 5 + 0],
                        state[i * 5 + 1],
                        state[i * 5 + 2],
                        state[i * 5 + 3],
                    ) = ATT(
                        x,
                        state[i * 5 + 0],
                        state[i * 5 + 1],
                        state[i * 5 + 2],
                        state[i * 5 + 3],
                        w[f"{bbb}ln1.weight"],
                        w[f"{bbb}ln1.bias"],
                        w[f"{att}time_mix_k"],
                        w[f"{att}time_mix_v"],
                        w[f"{att}time_mix_r"],
                        w[f"{att}time_decay"],
                        w[f"{att}time_first"],
                        kw,
                        vw,
                        rw,
                        ow,
                        kmx,
                        krx,
                        kmy,
                        kry,
                        vmx,
                        vrx,
                        vmy,
                        vry,
                        rmx,
                        rrx,
                        rmy,
                        rry,
                        omx,
                        orx,
                        omy,
                        ory,
                    )
                elif self.version == 5:
                    x, state[i * 3 + 0], state[i * 3 + 1] = ATT(
                        x,
                        state[i * 3 + 0],
                        state[i * 3 + 1],
                        w[f"{bbb}ln1.weight"],
                        w[f"{bbb}ln1.bias"],
                        w[f"{att}ln_x.weight"],
                        w[f"{att}ln_x.bias"],
                        w[f"{att}time_mix_k"],
                        w[f"{att}time_mix_v"],
                        w[f"{att}time_mix_r"],
                        w[f"{att}time_decay"],
                        w[f"{att}time_first"],
                        kw,
                        vw,
                        rw,
                        ow,
                        kmx,
                        krx,
                        kmy,
                        kry,
                        vmx,
                        vrx,
                        vmy,
                        vry,
                        rmx,
                        rrx,
                        rmy,
                        rry,
                        omx,
                        orx,
                        omy,
                        ory,
                    )
                elif self.version == 5.1 or self.version == 5.2:
                    x, state[i * 3 + 0], state[i * 3 + 1] = ATT(
                        x,
                        state[i * 3 + 0],
                        state[i * 3 + 1],
                        w[f"{bbb}ln1.weight"],
                        w[f"{bbb}ln1.bias"],
                        w[f"{att}ln_x.weight"],
                        w[f"{att}ln_x.bias"],
                        w[f"{att}time_mix_k"],
                        w[f"{att}time_mix_v"],
                        w[f"{att}time_mix_r"],
                        w[f"{att}time_mix_g"],
                        w[f"{att}time_decay"],
                        w[f"{att}time_first"],
                        kw,
                        vw,
                        rw,
                        gw,
                        ow,
                        kmx,
                        krx,
                        kmy,
                        kry,
                        vmx,
                        vrx,
                        vmy,
                        vry,
                        rmx,
                        rrx,
                        rmy,
                        rry,
                        gmx,
                        grx,
                        gmy,
                        gry,
                        omx,
                        orx,
                        omy,
                        ory,
                    )
                if dd.stream:
                    del kw, vw, rw, ow

                kw = w[f"{ffn}key.weight"]
                vw = w[f"{ffn}value.weight"]
                rw = w[f"{ffn}receptance.weight"]
                if dd.stream:
                    kw = kw.to(device=dev, non_blocking=True)
                    vw = vw.to(device=dev, non_blocking=True)
                    rw = rw.to(device=dev, non_blocking=True)
                kmx = w[f"{ffn}key.weight_mx"] if wtype == torch.uint8 else x
                krx = w[f"{ffn}key.weight_rx"] if wtype == torch.uint8 else x
                kmy = w[f"{ffn}key.weight_my"] if wtype == torch.uint8 else x
                kry = w[f"{ffn}key.weight_ry"] if wtype == torch.uint8 else x
                vmx = w[f"{ffn}value.weight_mx"] if wtype == torch.uint8 else x
                vrx = w[f"{ffn}value.weight_rx"] if wtype == torch.uint8 else x
                vmy = w[f"{ffn}value.weight_my"] if wtype == torch.uint8 else x
                vry = w[f"{ffn}value.weight_ry"] if wtype == torch.uint8 else x
                rmx = w[f"{ffn}receptance.weight_mx"] if wtype == torch.uint8 else x
                rrx = w[f"{ffn}receptance.weight_rx"] if wtype == torch.uint8 else x
                rmy = w[f"{ffn}receptance.weight_my"] if wtype == torch.uint8 else x
                rry = w[f"{ffn}receptance.weight_ry"] if wtype == torch.uint8 else x
                if self.version == 4:
                    offset = i * 5 + 4
                elif int(self.version) == 5:
                    offset = i * 3 + 2
                x, state[offset] = FFN(
                    x,
                    state[offset],
                    w[f"{bbb}ln2.weight"],
                    w[f"{bbb}ln2.bias"],
                    w[f"{ffn}time_mix_k"],
                    w[f"{ffn}time_mix_r"],
                    kw,
                    vw,
                    rw,
                    kmx,
                    krx,
                    kmy,
                    kry,
                    vmx,
                    vrx,
                    vmy,
                    vry,
                    rmx,
                    rrx,
                    rmy,
                    rry,
                )
                if dd.stream:
                    del kw, vw, rw

                if self.RESCALE_LAYER > 0:
                    if (i + 1) % self.RESCALE_LAYER == 0:
                        x = x / 2

            dd = self.strategy[args.n_layer]
            x = x[-1, :] if (seq_mode and (not full_output)) else x
            x = x.to(dtype=dd.atype, device=dd.device)

            x = F.layer_norm(
                x, (args.n_embd,), weight=w["ln_out.weight"], bias=w["ln_out.bias"]
            )
            if w["head.weight"].dtype != torch.uint8:
                x = x @ w["head.weight"]
            else:
                if seq_mode and full_output:
                    x = mm8_seq(
                        x,
                        w["head.weight"],
                        w["head.weight_mx"],
                        w["head.weight_rx"],
                        w["head.weight_my"],
                        w["head.weight_ry"],
                    )
                else:
                    x = mm8_one(
                        x,
                        w["head.weight"],
                        w["head.weight_mx"],
                        w["head.weight_rx"],
                        w["head.weight_my"],
                        w["head.weight_ry"],
                    )

            return x.float(), state
