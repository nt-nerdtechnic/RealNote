from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import torch
import torchaudio
from transformers import AutomaticSpeechRecognitionPipeline, WhisperForConditionalGeneration, WhisperProcessor


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _load_waveform(audio_path: Path) -> tuple[Any, int]:
    waveform, sample_rate = torchaudio.load(str(audio_path))

    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)

    if sample_rate != 16_000:
        resampler = torchaudio.transforms.Resample(sample_rate, 16_000)
        waveform = resampler(waveform)
        sample_rate = 16_000

    return waveform.squeeze().numpy(), sample_rate


class BreezeAsr:
    def __init__(self, model_id: str | None = None) -> None:
        self.model_id = model_id or os.getenv("BREEZE_ASR_MODEL", "MediaTek-Research/Breeze-ASR-25")
        self.device = detect_device()
        self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32

        self.processor = WhisperProcessor.from_pretrained(self.model_id)
        self.model = WhisperForConditionalGeneration.from_pretrained(
            self.model_id,
            torch_dtype=self.torch_dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
        )
        self.model.to(self.device).eval()

        self.pipeline = AutomaticSpeechRecognitionPipeline(
            model=self.model,
            tokenizer=self.processor.tokenizer,
            feature_extractor=self.processor.feature_extractor,
            chunk_length_s=30,
            stride_length_s=5,
            torch_dtype=self.torch_dtype,
            device=self.device,
        )

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        waveform, _ = _load_waveform(audio_path)
        result = self.pipeline(
            waveform,
            return_timestamps=True,
            generate_kwargs={"task": "transcribe", "language": "zh"},
        )
        return result
