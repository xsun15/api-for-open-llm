import warnings
from typing import Optional

import torch
from peft import PeftModel
from transformers import (
    AutoModel,
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)


def get_gpu_memory(max_gpus=None):
    """Get available memory for each GPU."""
    gpu_memory = []
    num_gpus = (
        torch.cuda.device_count()
        if max_gpus is None
        else min(max_gpus, torch.cuda.device_count())
    )

    for gpu_id in range(num_gpus):
        with torch.cuda.device(gpu_id):
            device = torch.cuda.current_device()
            gpu_properties = torch.cuda.get_device_properties(device)
            total_memory = gpu_properties.total_memory / (1024 ** 3)
            allocated_memory = torch.cuda.memory_allocated() / (1024 ** 3)
            available_memory = total_memory - allocated_memory
            gpu_memory.append(available_memory)
    return gpu_memory


# A global registry for all model adapters
model_adapters = []


def register_model_adapter(cls):
    """Register a model adapter."""
    model_adapters.append(cls())


def get_model_adapter(model_name: str):
    """Get a model adapter for a model_name."""
    for adapter in model_adapters:
        if adapter.match(model_name):
            return adapter
    raise ValueError(f"No valid model adapter for {model_name}")


class BaseModelAdapter:
    """The base and the default model adapter."""

    def match(self, model_name):
        return True

    def load_model(self, model_name_or_path: Optional[str] = None, adapter_model: Optional[str] = None, **kwargs):
        """ Load model through transformers. """
        model_name_or_path = self.default_model_name_or_path if model_name_or_path is None else model_name_or_path
        tokenizer_kwargs = self.tokenizer_kwargs
        if adapter_model is not None:
            try:
                tokenizer = self.tokenizer_class.from_pretrained(adapter_model, **tokenizer_kwargs)
            except OSError:
                tokenizer = self.tokenizer_class.from_pretrained(model_name_or_path, **tokenizer_kwargs)
        else:
            tokenizer = self.tokenizer_class.from_pretrained(model_name_or_path, **tokenizer_kwargs)

        device = kwargs.get("device", "cuda")
        num_gpus = kwargs.get("num_gpus", 1)
        load_in_8bit = kwargs.get("load_in_8bit", False)
        load_in_4bit = kwargs.get("load_in_4bit", False)

        model_kwargs = self.model_kwargs
        if device == "cpu":
            model_kwargs["torch_dtype"] = torch.float32
        else:
            if "torch_dtype" not in model_kwargs:
                model_kwargs["torch_dtype"] = torch.float16

            if num_gpus != 1:
                model_kwargs["device_map"] = "auto"
                model_kwargs["device_map"] = "sequential"  # This is important for not the same VRAM sizes
                available_gpu_memory = get_gpu_memory(num_gpus)
                model_kwargs["max_memory"] = {
                    i: str(int(available_gpu_memory[i] * 0.85)) + "GiB"
                    for i in range(num_gpus)
                }

            if load_in_8bit or load_in_4bit:
                if num_gpus != 1:
                    warnings.warn(
                        "8-bit/4-bit quantization is not supported for multi-gpu inference."
                    )
                model_kwargs["torch_dtype"] = None
                model_kwargs["load_in_8bit"] = load_in_8bit
                model_kwargs["load_in_4bit"] = load_in_4bit
                model_kwargs["device_map"] = "auto"

        if load_in_4bit:
            model = self.model_class.from_pretrained(
                model_name_or_path,
                torch_dtype=torch.bfloat16,
                quantization_config=BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.bfloat16,
                    bnb_4bit_use_double_quant=True,
                    bnb_4bit_quant_type='nf4'
                ),
            )
        else:
            model = self.model_class.from_pretrained(
                model_name_or_path,
                **model_kwargs
            )

        # post process for special tokens
        tokenizer = self.post_tokenizer(tokenizer)
        is_chatglm = "chatglm" in str(type(model))

        if adapter_model is not None:
            if not is_chatglm:
                model_vocab_size = model.get_input_embeddings().weight.size(0)
                tokenzier_vocab_size = len(tokenizer)
                print(f"Vocab of the base model: {model_vocab_size}")
                print(f"Vocab of the tokenizer: {tokenzier_vocab_size}")

                if model_vocab_size != tokenzier_vocab_size:
                    assert tokenzier_vocab_size > model_vocab_size
                    print("Resize model embeddings to fit tokenizer")
                    model.resize_token_embeddings(tokenzier_vocab_size)

            model = self.load_adapter_model(model, adapter_model, model_kwargs)

        if is_chatglm:
            quantize = kwargs.get("quantize", None)
            if quantize and quantize != 16:
                model = model.quantize(quantize)

        if device != "cpu" and not load_in_4bit and not load_in_8bit and num_gpus == 1:
            model.to(device)

        model.eval()

        return model, tokenizer

    def load_adapter_model(self, model, adapter_model, model_kwargs):
        return PeftModel.from_pretrained(
            model,
            adapter_model,
            torch_dtype=model_kwargs.get("torch_dtype", torch.float16),
        )

    def post_tokenizer(self, tokenizer):
        return tokenizer

    @property
    def model_class(self):
        return AutoModelForCausalLM

    @property
    def model_kwargs(self):
        return {}

    @property
    def tokenizer_class(self):
        return AutoTokenizer

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": False}

    @property
    def default_model_name_or_path(self):
        return "zpn/llama-7b"


def load_model(
    model_name: str,
    model_name_or_path: Optional[str] = None,
    adapter_model: Optional[str] = None,
    quantize: Optional[int] = 16,
    device: Optional[str] = "cuda",
    load_in_8bit: Optional[bool] = False,
    **kwargs
):
    model_name = model_name.lower()
    adapter = get_model_adapter(model_name)
    model, tokenizer = adapter.load_model(
        model_name_or_path,
        adapter_model,
        device=device,
        quantize=quantize,
        load_in_8bit=load_in_8bit,
        **kwargs
    )
    return model, tokenizer


class ChatglmModelAdapter(BaseModelAdapter):

    def match(self, model_name):
        return "chatglm" in model_name

    @property
    def model_class(self):
        return AutoModel

    @property
    def model_kwargs(self):
        return {"trust_remote_code": True}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": False, "trust_remote_code": True}

    @property
    def default_model_name_or_path(self):
        return "THUDM/chatglm-6b"


class LlamaModelAdapter(BaseModelAdapter):

    def match(self, model_name):
        return "alpaca" in model_name or "baize" in model_name or "guanaco" in model_name

    def post_tokenizer(self, tokenizer):
        tokenizer.bos_token = "<s>"
        tokenizer.eos_token = "</s>"
        tokenizer.unk_token = "<unk>"
        return tokenizer

    @property
    def model_kwargs(self):
        return {"low_cpu_mem_usage": True}


class MossModelAdapter(BaseModelAdapter):

    def match(self, model_name):
        return "moss" in model_name

    @property
    def model_kwargs(self):
        return {"trust_remote_code": True}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": False, "trust_remote_code": True}

    @property
    def default_model_name_or_path(self):
        return "fnlp/moss-moon-003-sft-int4"


class PhoenixModelAdapter(BaseModelAdapter):

    def match(self, model_name):
        return "phoenix" in model_name

    @property
    def model_kwargs(self):
        return {"low_cpu_mem_usage": True}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": True}

    @property
    def default_model_name_or_path(self):
        return "FreedomIntelligence/phoenix-inst-chat-7b"


class FireflyModelAdapter(BaseModelAdapter):

    def match(self, model_name):
        return "firefly" in model_name

    @property
    def model_kwargs(self):
        return {"torch_dtype": torch.float32}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": True}

    @property
    def default_model_name_or_path(self):
        return "YeungNLP/firefly-2b6"


register_model_adapter(ChatglmModelAdapter)
register_model_adapter(LlamaModelAdapter)
register_model_adapter(MossModelAdapter)
register_model_adapter(PhoenixModelAdapter)
register_model_adapter(FireflyModelAdapter)

register_model_adapter(BaseModelAdapter)
