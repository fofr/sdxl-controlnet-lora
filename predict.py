from typing import List, Optional, Union
from cog import BasePredictor, Input, Path
import os
import time
import numpy as np
import torch
from diffusers import DiffusionPipeline

from diffusers import (
    DDIMScheduler,
    DiffusionPipeline,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    PNDMScheduler,
    StableDiffusionXLImg2ImgPipeline,
    StableDiffusionXLInpaintPipeline,
    StableDiffusionXLControlNetPipeline,
    StableDiffusionXLControlNetInpaintPipeline,
    StableDiffusionXLControlNetImg2ImgPipeline,
)

from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker,
)
from transformers import CLIPImageProcessor
from weights_downloader import WeightsDownloader
from weights_manager import WeightsManager
from controlnet import ControlNet
from sizing_strategy import SizingStrategy
import logging
from dotenv import load_dotenv
from huggingface_hub import login

# Configure logging
logging.basicConfig(level=logging.INFO)

# Load environment variables from the .env file
load_dotenv()

# Retrieve the token from the environment variables
hf_token = os.getenv("HF_TOKEN")

if not hf_token:
    logging.error(
        "HF_TOKEN is not set. Please ensure it's specified in your environment."
    )
else:
    try:
        login(token=hf_token)
        logging.info("Login succeeded")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")

SDXL_MODEL_CACHE = "./sdxl-cache"
REFINER_MODEL_CACHE = "./refiner-cache"
SAFETY_CACHE = "./safety-cache"
FEATURE_EXTRACTOR = "./feature-extractor"
SDXL_URL = "https://weights.replicate.delivery/default/sdxl/sdxl-vae-upcast-fix.tar"
REFINER_URL = (
    "https://weights.replicate.delivery/default/sdxl/refiner-no-vae-no-encoder-1.0.tar"
)
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"

JOURNA_CONTAINER_NAME = os.getenv("JOURNA_CONTAINER_NAME")
JOURNA_MODEL_CACHE = "./journa_cache"
SAS_TOKEN = os.getenv("SAS_TOKEN")


class KarrasDPM:
    def from_config(config):
        return DPMSolverMultistepScheduler.from_config(config, use_karras_sigmas=True)


SCHEDULERS = {
    "DDIM": DDIMScheduler,
    "DPMSolverMultistep": DPMSolverMultistepScheduler,
    "HeunDiscrete": HeunDiscreteScheduler,
    "KarrasDPM": KarrasDPM,
    "K_EULER_ANCESTRAL": EulerAncestralDiscreteScheduler,
    "K_EULER": EulerDiscreteScheduler,
    "PNDM": PNDMScheduler,
}


class Predictor(BasePredictor):
    def load_trained_weights(self, weights_list, scales_list):
        self.weights_manager.load_trained_weights(weights_list, scales_list)

    def setup(self, weights: Optional[Path] = None, lora_weights: Optional[str] = None):
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()
        self.sizing_strategy = SizingStrategy()
        self.weights_manager = WeightsManager(self)
        self.tuned_model = False
        self.tuned_weights = None
        if str(weights) == "weights":
            weights = None

        print("Loading safety checker...")
        WeightsDownloader.download_if_not_exists(SAFETY_URL, SAFETY_CACHE)

        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_CACHE, torch_dtype=torch.float16
        ).to("cuda")
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)

        WeightsDownloader.download_if_not_exists(SDXL_URL, SDXL_MODEL_CACHE)

        print("Loading sdxl txt2img pipeline...")
        self.txt2img_pipe = DiffusionPipeline.from_pretrained(
            SDXL_MODEL_CACHE,
            torch_dtype=torch.float16,
            use_safetensors=True,
            variant="fp16",
        ).to("cuda")

        self.is_lora = False
        if weights or os.path.exists("./trained-model"):
            self.load_trained_weights([weights], [1.0])
            print(f"Successfully loaded: {weights}")

        print("Loading SDXL img2img pipeline...")
        self.img2img_pipe = StableDiffusionXLImg2ImgPipeline(
            vae=self.txt2img_pipe.vae,
            text_encoder=self.txt2img_pipe.text_encoder,
            text_encoder_2=self.txt2img_pipe.text_encoder_2,
            tokenizer=self.txt2img_pipe.tokenizer,
            tokenizer_2=self.txt2img_pipe.tokenizer_2,
            unet=self.txt2img_pipe.unet,
            scheduler=self.txt2img_pipe.scheduler,
        ).to("cuda")

        print("Loading SDXL inpaint pipeline...")
        self.inpaint_pipe = StableDiffusionXLInpaintPipeline(
            vae=self.txt2img_pipe.vae,
            text_encoder=self.txt2img_pipe.text_encoder,
            text_encoder_2=self.txt2img_pipe.text_encoder_2,
            tokenizer=self.txt2img_pipe.tokenizer,
            tokenizer_2=self.txt2img_pipe.tokenizer_2,
            unet=self.txt2img_pipe.unet,
            scheduler=self.txt2img_pipe.scheduler,
        ).to("cuda")

        print("Loading SDXL refiner pipeline...")
        WeightsDownloader.download_if_not_exists(REFINER_URL, REFINER_MODEL_CACHE)

        self.refiner = DiffusionPipeline.from_pretrained(
            REFINER_MODEL_CACHE,
            text_encoder_2=self.txt2img_pipe.text_encoder_2,
            vae=self.txt2img_pipe.vae,
            torch_dtype=torch.float16,
            use_safetensors=True,
            variant="fp16",
        ).to("cuda")

        self.controlnet = ControlNet(self)

        # Download and load Journa LoRA weights if specified
        try:
            blob_service_client = WeightsDownloader.get_blob_service_client_sas(
                SAS_TOKEN
            )
            WeightsDownloader.download_blobs_in_container(
                blob_service_client, JOURNA_CONTAINER_NAME, JOURNA_MODEL_CACHE
            )
        except Exception as e:
            logging.error(f"Failed to download Journa LoRA weights: {e}")
            raise RuntimeError("Could not download and load Journa LoRA weights") from e

        print("setup took: ", time.time() - start)

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(
            description="Input prompt", default="An astronaut riding a rainbow unicorn"
        ),
        negative_prompt: str = Input(description="Negative Prompt", default=""),
        image: Path = Input(
            description="Input image for img2img or inpaint mode", default=None
        ),
        mask: Path = Input(
            description="Input mask for inpaint mode. Black areas will be preserved, white areas will be inpainted.",
            default=None,
        ),
        width: int = Input(description="Width of output image", default=1024),
        height: int = Input(description="Height of output image", default=1024),
        sizing_strategy: str = Input(
            description="Decide how to resize images – use width/height, resize based on input image or control image",
            choices=[
                "width_height",
                "input_image",
                "controlnet_1_image",
                "controlnet_2_image",
                "controlnet_3_image",
                "mask_image",
            ],
            default="width_height",
        ),
        num_outputs: int = Input(
            description="Number of images to output", ge=1, le=16, default=1
        ),
        scheduler: str = Input(
            description="scheduler", choices=SCHEDULERS.keys(), default="K_EULER"
        ),
        num_inference_steps: int = Input(
            description="Number of denoising steps", ge=1, le=500, default=30
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance", ge=1, le=50, default=7.5
        ),
        prompt_strength: float = Input(
            description="Prompt strength when using img2img / inpaint. 1.0 corresponds to full destruction of information in image",
            ge=0.0,
            le=1.0,
            default=0.8,
        ),
        seed: int = Input(
            description="Random seed. Leave blank to randomize the seed", default=None
        ),
        refine: str = Input(
            description="Which refine style to use",
            choices=["no_refiner", "base_image_refiner"],
            default="no_refiner",
        ),
        refine_steps: int = Input(
            description="For base_image_refiner, the number of steps to refine, defaults to num_inference_steps",
            default=None,
        ),
        apply_watermark: bool = Input(
            description="Applies a watermark to enable determining if an image is generated in downstream applications. If you have other provisions for generating or deploying images safely, you can use this to disable watermarking.",
            default=True,
        ),
        setup_lora_weight: str = Input(
            description="LoRA weight to be loaded during setup", default=None
        ),
        setup_lora_scale: float = Input(
            description="Scale for LoRA weight loaded during setup",
            ge=0.0,
            le=1.0,
            default=0.6,
        ),
        predict_lora_weight: Optional[str] = Input(
            description="LoRA weight to be loaded during prediction", default=None
        ),
        predict_lora_scale: float = Input(
            description="Scale for LoRA weight loaded during prediction",
            ge=0.0,
            le=1.0,
            default=0.6,
        ),
        disable_safety_checker: bool = Input(
            description="Disable safety checker for generated images. This feature is only available through the API. See [https://replicate.com/docs/how-does-replicate-work#safety](https://replicate.com/docs/how-does-replicate-work#safety)",
            default=False,
        ),
        controlnet_1: str = Input(
            description="Controlnet",
            choices=ControlNet.CONTROLNET_MODELS,
            default="none",
        ),
        controlnet_1_image: Path = Input(
            description="Input image for first controlnet", default=None
        ),
        controlnet_1_conditioning_scale: float = Input(
            description="How strong the controlnet conditioning is",
            ge=0.0,
            le=4.0,
            default=0.75,
        ),
        controlnet_1_start: float = Input(
            description="When controlnet conditioning starts",
            ge=0.0,
            le=1.0,
            default=0.0,
        ),
        controlnet_1_end: float = Input(
            description="When controlnet conditioning ends", ge=0.0, le=1.0, default=1.0
        ),
        controlnet_2: str = Input(
            description="Controlnet",
            choices=ControlNet.CONTROLNET_MODELS,
            default="none",
        ),
        controlnet_2_image: Path = Input(
            description="Input image for second controlnet", default=None
        ),
        controlnet_2_conditioning_scale: float = Input(
            description="How strong the controlnet conditioning is",
            ge=0.0,
            le=4.0,
            default=0.75,
        ),
        controlnet_2_start: float = Input(
            description="When controlnet conditioning starts",
            ge=0.0,
            le=1.0,
            default=0.0,
        ),
        controlnet_2_end: float = Input(
            description="When controlnet conditioning ends", ge=0.0, le=1.0, default=1.0
        ),
        controlnet_3: str = Input(
            description="Controlnet",
            choices=ControlNet.CONTROLNET_MODELS,
            default="none",
        ),
        controlnet_3_image: Path = Input(
            description="Input image for third controlnet", default=None
        ),
        controlnet_3_conditioning_scale: float = Input(
            description="How strong the controlnet conditioning is",
            ge=0.0,
            le=4.0,
            default=0.75,
        ),
        controlnet_3_start: float = Input(
            description="When controlnet conditioning starts",
            ge=0.0,
            le=1.0,
            default=0.0,
        ),
        controlnet_3_end: float = Input(
            description="When controlnet conditioning ends", ge=0.0, le=1.0, default=1.0
        ),
    ) -> List[Path]:
        """Run a single prediction on the model."""
        predict_start = time.time()

        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        resize_start = time.time()
        width, height, resized_images = self.sizing_strategy.apply(
            sizing_strategy,
            width,
            height,
            image,
            mask,
            controlnet_1_image,
            controlnet_2_image,
            controlnet_3_image,
        )
        print(f"resize took: {time.time() - resize_start:.2f}s")

        (
            image,
            mask,
            controlnet_1_image,
            controlnet_2_image,
            controlnet_3_image,
        ) = resized_images

        if setup_lora_weight:
            lora_load_start = time.time()
            self.weights_manager.load_lora_weight(
                setup_lora_weight, setup_lora_scale, "setup_lora"
            )
            print(f"Setup LoRA load took: {time.time() - lora_load_start:.2f}s")

        if predict_lora_weight:
            lora_load_start = time.time()
            self.weights_manager.load_lora_weight(
                predict_lora_weight, predict_lora_scale, "predict_lora"
            )
            print(f"Prediction LoRA load took: {time.time() - lora_load_start:.2f}s")

        # OOMs can leave vae in bad state
        if self.txt2img_pipe.vae.dtype == torch.float32:
            self.txt2img_pipe.vae.to(dtype=torch.float16)

        sdxl_kwargs = {}
        if self.tuned_model:
            for k, v in self.token_map.items():
                prompt = prompt.replace(k, v)
        print(f"Prompt: {prompt}")

        inpainting = image and mask
        img2img = image and not mask
        controlnet = (
            controlnet_1 != "none" or controlnet_2 != "none" or controlnet_3 != "none"
        )

        controlnet_args = {}
        control_images = []
        if controlnet:
            controlnet_conditioning_scales = []
            control_guidance_start = []
            control_guidance_end = []

            controlnets = [
                (
                    controlnet_1,
                    controlnet_1_conditioning_scale,
                    controlnet_1_start,
                    controlnet_1_end,
                    controlnet_1_image,
                ),
                (
                    controlnet_2,
                    controlnet_2_conditioning_scale,
                    controlnet_2_start,
                    controlnet_2_end,
                    controlnet_2_image,
                ),
                (
                    controlnet_3,
                    controlnet_3_conditioning_scale,
                    controlnet_3_start,
                    controlnet_3_end,
                    controlnet_3_image,
                ),
            ]

            controlnet_preprocess_start = time.time()
            for controlnet in controlnets:
                if controlnet[0] != "none":
                    controlnet_conditioning_scales.append(controlnet[1])
                    control_guidance_start.append(controlnet[2])
                    control_guidance_end.append(controlnet[3])
                    annotated_image = self.controlnet.preprocess(
                        controlnet[4], controlnet[0]
                    )
                    control_images.append(annotated_image)
            print(
                f"controlnet preprocess took: {time.time() - controlnet_preprocess_start:.2f}s"
            )

            controlnet_args = {
                "controlnet_conditioning_scale": controlnet_conditioning_scales,
                "control_guidance_start": control_guidance_start,
                "control_guidance_end": control_guidance_end,
            }

            if inpainting:
                print("Using inpaint + controlnet pipeline")
                controlnet_args["control_image"] = control_images
                pipe = self.build_controlnet_pipeline(
                    StableDiffusionXLControlNetInpaintPipeline,
                    [controlnet[0] for controlnet in controlnets],
                )
            elif img2img:
                print("Using img2img + controlnet pipeline")
                controlnet_args["control_image"] = control_images
                pipe = self.build_controlnet_pipeline(
                    StableDiffusionXLControlNetImg2ImgPipeline,
                    [controlnet[0] for controlnet in controlnets],
                )
            else:
                print("Using txt2img + controlnet pipeline")
                controlnet_args["image"] = control_images
                pipe = self.build_controlnet_pipeline(
                    StableDiffusionXLControlNetPipeline,
                    [controlnet[0] for controlnet in controlnets],
                )

        elif inpainting:
            print("Using inpaint pipeline")
            pipe = self.inpaint_pipe
        elif img2img:
            print("Using img2img pipeline")
            pipe = self.img2img_pipe
        else:
            print("Using txt2img pipeline")
            pipe = self.txt2img_pipe

        if inpainting:
            sdxl_kwargs["image"] = image
            sdxl_kwargs["mask_image"] = mask
            sdxl_kwargs["strength"] = prompt_strength
        elif img2img:
            sdxl_kwargs["image"] = image
            sdxl_kwargs["strength"] = prompt_strength

        if refine == "base_image_refiner":
            sdxl_kwargs["output_type"] = "latent"

        if not apply_watermark:
            watermark_cache = pipe.watermark
            pipe.watermark = None
            self.refiner.watermark = None

        pipe.scheduler = SCHEDULERS[scheduler].from_config(pipe.scheduler.config)
        generator = torch.Generator("cuda").manual_seed(seed)

        common_args = {
            "prompt": [prompt] * num_outputs,
            "negative_prompt": [negative_prompt] * num_outputs,
            "guidance_scale": guidance_scale,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
        }

        if controlnet or not img2img:
            common_args["width"] = width
            common_args["height"] = height

        inference_start = time.time()
        output = pipe(
            **common_args,
            **sdxl_kwargs,
            **controlnet_args,
            cross_attention_kwargs={
                "scale": 1.0
            },  # Use a default scale, adjusted by LoRA scales
        )
        print(f"inference took: {time.time() - inference_start:.2f}s")

        if refine == "base_image_refiner":
            refiner_kwargs = {"image": output.images}
            common_args_without_dimensions = {
                k: v for k, v in common_args.items() if k not in ["width", "height"]
            }

            if refine == "base_image_refiner" and refine_steps:
                common_args_without_dimensions["num_inference_steps"] = refine_steps

            output = self.refiner(**common_args_without_dimensions, **refiner_kwargs)

        if not apply_watermark:
            pipe.watermark = watermark_cache
            self.refiner.watermark = watermark_cache

        if not disable_safety_checker:
            _, has_nsfw_content = self.run_safety_checker(output.images)

        output_paths = []

        if controlnet:
            for i, image in enumerate(control_images):
                output_path = f"/tmp/control-{i}.png"
                image.save(output_path)
                output_paths.append(Path(output_path))

        for i, image in enumerate(output.images):
            if not disable_safety_checker:
                if has_nsfw_content[i]:
                    print(f"NSFW content detected in image {i}")
                    continue
            output_path = f"/tmp/out-{i}.png"
            image.save(output_path)
            output_paths.append(Path(output_path))

        if len(output_paths) == 0:
            raise Exception(
                "NSFW content detected. Try running it again, or try a different prompt."
            )

        print(f"prediction took: {time.time() - predict_start:.2f}s")
        return output_paths
