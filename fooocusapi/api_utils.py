import base64
import io
from io import BytesIO
from typing import List

import numpy as np
from fastapi import Response, UploadFile
from PIL import Image
from fooocusapi.file_utils import get_file_serve_url, output_file_to_base64img, output_file_to_bytesimg
from fooocusapi.models import AsyncJobResponse, AsyncJobStage, GeneratedImageResult, GenerationFinishReason, ImgInpaintOrOutpaintRequest, ImgPromptRequest, ImgUpscaleOrVaryRequest, Text2ImgRequest
from fooocusapi.parameters import ImageGenerationParams, ImageGenerationResult, available_aspect_ratios, default_aspect_ratio, inpaint_model_version, default_sampler, default_scheduler, default_base_model_name, default_refiner_model_name
from fooocusapi.task_queue import QueueTask
import modules.flags as flags
import modules.config as path
from modules.sdxl_styles import legal_style_names


def narray_to_base64img(narray: np.ndarray) -> str:
    if narray is None:
        return None

    img = Image.fromarray(narray)
    output_buffer = BytesIO()
    img.save(output_buffer, format='PNG')
    byte_data = output_buffer.getvalue()
    base64_str = base64.b64encode(byte_data)
    return base64_str


def narray_to_bytesimg(narray) -> bytes:
    if narray is None:
        return None

    img = Image.fromarray(narray)
    output_buffer = BytesIO()
    img.save(output_buffer, format='PNG')
    byte_data = output_buffer.getvalue()
    return byte_data


def read_input_image(input_image: UploadFile) -> np.ndarray:
    input_image_bytes = input_image.file.read()
    pil_image = Image.open(io.BytesIO(input_image_bytes))
    image = np.array(pil_image)
    return image


def req_to_params(req: Text2ImgRequest) -> ImageGenerationParams:
    if req.base_model_name is not None:
        if req.base_model_name not in path.model_filenames:
            print(f"[Warning] Wrong base_model_name input: {req.base_model_name}, using default")
            req.base_model_name = default_base_model_name

    if req.refiner_model_name is not None and req.refiner_model_name != 'None':
        if req.refiner_model_name not in path.model_filenames:
            print(f"[Warning] Wrong refiner_model_name input: {req.refiner_model_name}, using default")
            req.refiner_model_name = default_refiner_model_name

    for l in req.loras:
        if l.model_name != 'None' and l.model_name not in path.lora_filenames:
            print(f"[Warning] Wrong lora model_name input: {l.model_name}, using 'None'")
            l.model_name = 'None'

    prompt = req.prompt
    negative_prompt = req.negative_prompt
    style_selections = [
        s for s in req.style_selections if s in legal_style_names]
    performance_selection = req.performance_selection.value
    aspect_ratios_selection = req.aspect_ratios_selection
    image_number = req.image_number
    image_seed = None if req.image_seed == -1 else req.image_seed
    sharpness = req.sharpness
    guidance_scale = req.guidance_scale
    base_model_name = req.base_model_name
    refiner_model_name = req.refiner_model_name
    refiner_switch = req.refiner_switch
    loras = [(lora.model_name, lora.weight) for lora in req.loras]
    uov_input_image = None if not isinstance(
        req, ImgUpscaleOrVaryRequest) else read_input_image(req.input_image)
    uov_method = flags.disabled if not isinstance(
        req, ImgUpscaleOrVaryRequest) else req.uov_method.value
    outpaint_selections = [] if not isinstance(req, ImgInpaintOrOutpaintRequest) else [
        s.value for s in req.outpaint_selections]
    
    if aspect_ratios_selection not in available_aspect_ratios:
        print(f"[Warning] Invalid aspect ratios selection, using default: {default_aspect_ratio}")
        aspect_ratios_selection = default_aspect_ratio
    
    if refiner_model_name == '':
        refiner_model_name = 'None'

    inpaint_input_image = None
    if isinstance(req, ImgInpaintOrOutpaintRequest):
        input_image = read_input_image(req.input_image)
        if req.input_mask is not None:
            input_mask = read_input_image(req.input_mask)
        else:
            input_mask = np.zeros(input_image.shape)
        inpaint_input_image = {
            'image': input_image,
            'mask': input_mask
        }

    image_prompts = []
    if isinstance(req, ImgPromptRequest):
        for img_prompt in req.image_prompts:
            if img_prompt.cn_img is not None:
                cn_img = read_input_image(img_prompt.cn_img)
                image_prompts.append(
                    (cn_img, img_prompt.cn_stop, img_prompt.cn_weight, img_prompt.cn_type.value))
                
    advanced_params = None
    if req.advanced_params is not None:
        adp = req.advanced_params

        if adp.refiner_swap_method not in ['joint', 'separate', 'vae']:
            print(f"[Warning] Wrong refiner_swap_method input: {adp.refiner_swap_method}, using default")
            adp.refiner_swap_method = 'joint'

        if adp.sampler_name not in flags.sampler_list:
            print(f"[Warning] Wrong sampler_name input: {adp.sampler_name}, using default")
            adp.sampler_name = default_sampler

        if adp.scheduler_name not in flags.scheduler_list:
            print(f"[Warning] Wrong scheduler_name input: {adp.scheduler_name}, using default")
            adp.scheduler_name = default_scheduler

        if adp.inpaint_engine not in ['v1', 'v2.5']:
            print(f"[Warning] Wrong inpaint_engine input: {adp.inpaint_engine}, using default")
            adp.inpaint_engine = inpaint_model_version
        
        advanced_params = [adp.adm_scaler_positive, adp.adm_scaler_negative, adp.adm_scaler_end, adp.adaptive_cfg, adp.sampler_name,
                                adp.scheduler_name, False, adp.overwrite_step, adp.overwrite_switch, adp.overwrite_width, adp.overwrite_height,
                                adp.overwrite_vary_strength, adp.overwrite_upscale_strength,
                                adp.mixing_image_prompt_and_vary_upscale, adp.mixing_image_prompt_and_inpaint,
                                adp.debugging_cn_preprocessor, adp.controlnet_softness, adp.canny_low_threshold, adp.canny_high_threshold, adp.inpaint_engine,
                                adp.refiner_swap_method, adp.freeu_enabled, adp.freeu_b1, adp.freeu_b2, adp.freeu_s1, adp.freeu_s2]

    return ImageGenerationParams(prompt=prompt,
                                 negative_prompt=negative_prompt,
                                 style_selections=style_selections,
                                 performance_selection=performance_selection,
                                 aspect_ratios_selection=aspect_ratios_selection,
                                 image_number=image_number,
                                 image_seed=image_seed,
                                 sharpness=sharpness,
                                 guidance_scale=guidance_scale,
                                 base_model_name=base_model_name,
                                 refiner_model_name=refiner_model_name,
                                 refiner_switch=refiner_switch,
                                 loras=loras,
                                 uov_input_image=uov_input_image,
                                 uov_method=uov_method,
                                 outpaint_selections=outpaint_selections,
                                 inpaint_input_image=inpaint_input_image,
                                 image_prompts=image_prompts,
                                 advanced_params=advanced_params,
                                 )


def generation_output(results: QueueTask | List[ImageGenerationResult], streaming_output: bool, require_base64: bool) -> Response | List[GeneratedImageResult] | AsyncJobResponse:
    if isinstance(results, QueueTask):
        task = results
        job_stage = AsyncJobStage.running
        job_result = None
        if task.start_millis == 0:
            job_stage = AsyncJobStage.waiting
        if task.is_finished:
            if task.finish_with_error:
                job_stage = AsyncJobStage.error
            else:
                if task.task_result != None:
                    job_stage = AsyncJobStage.success
                    task_result_require_base64 = False
                    if 'require_base64' in task.req_param and task.req_param['require_base64']:
                        task_result_require_base64 = True

                    job_result = generation_output(task.task_result, False, task_result_require_base64)
        return AsyncJobResponse(job_id=task.seq,
                                job_type=task.type,
                                job_stage=job_stage,
                                job_progess=task.finish_progess,
                                job_status=task.task_status,
                                job_result=job_result)

    if streaming_output:
        if len(results) == 0 or results[0].finish_reason != GenerationFinishReason.success:
            return Response(status_code=500)
        bytes = output_file_to_bytesimg(results[0].im)
        return Response(bytes, media_type='image/png')
    else:
        results = [GeneratedImageResult(
            base64=output_file_to_base64img(
                item.im) if require_base64 else None,
            url=get_file_serve_url(item.im),
            seed=item.seed,
            finish_reason=item.finish_reason) for item in results]
        return results


class QueueReachLimitException(Exception):
    pass
