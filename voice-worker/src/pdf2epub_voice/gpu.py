"""BlueTTS on an NVIDIA GPU.

ONNX Runtime comes as a CPU build and a GPU build that install the same `onnxruntime` package, so only one of
them can be in the environment. The CPU build is (the server has no GPU); on a computer with an NVIDIA GPU the
installer puts the GPU build in a folder of its own, RUNTIME, which goes first on the import path here. That
build is the last one made for CUDA 12, whose libraries come with PyTorch (installed for Chatterbox).

On this kind of model the GPU is about 3.5 times faster than a 14-core laptop processor, but only with
CUDA_OPTIONS: without the padding option some sentence lengths take 30 times longer than others.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUNTIME = Path(sys.prefix) / "onnxruntime-gpu"
CUDA_OPTIONS = {"cudnn_conv_algo_search": "HEURISTIC", "cudnn_conv1d_pad_to_nc1d": "1"}


def prefer_gpu_runtime() -> bool:
    """Put the GPU build of ONNX Runtime first on the import path when it is installed and a GPU is there.
    Call it before anything imports onnxruntime."""
    if not RUNTIME.is_dir() or "onnxruntime" in sys.modules:
        return False
    try:
        import torch  # its CUDA and cuDNN libraries are the ones the GPU build loads
    except Exception:  # noqa: BLE001 -- no PyTorch, no CUDA libraries
        return False
    if not torch.cuda.is_available():
        return False
    sys.path.insert(0, str(RUNTIME))
    return True


def providers() -> list | None:
    """ONNX Runtime providers for the GPU, or None when the GPU build is not the one in use."""
    import onnxruntime as ort

    if not Path(ort.__file__).is_relative_to(RUNTIME) or "CUDAExecutionProvider" not in ort.get_available_providers():
        return None
    return [("CUDAExecutionProvider", CUDA_OPTIONS), "CPUExecutionProvider"]
