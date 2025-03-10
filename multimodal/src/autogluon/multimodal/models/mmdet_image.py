import logging
import os
import time
import warnings
from typing import Optional

import torch
from torch import nn

try:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import mmcv
    from mmcv.ops import RoIPool
    from mmcv.parallel import scatter
    from mmcv.runner import load_checkpoint
except ImportError as e:
    mmcv = None

try:
    import mmdet
    from mmdet.core import get_classes
    from mmdet.models import build_detector
except ImportError as e:
    mmdet = None

from ..constants import (
    AUTOMM,
    BBOX,
    BBOX_FORMATS,
    COLUMN,
    COLUMN_FEATURES,
    FEATURES,
    IMAGE,
    IMAGE_VALID_NUM,
    LABEL,
    LOGITS,
    MASKS,
    XYWH,
    XYXY,
)
from .utils import lookup_mmdet_config, update_mmdet_config

logger = logging.getLogger(__name__)


class MMDetAutoModelForObjectDetection(nn.Module):
    """
    Support MMDET object detection models.
    Refer to https://github.com/open-mmlab/mmdetection
    """

    def __init__(
        self,
        prefix: str,
        checkpoint_name: str,
        config_file: Optional[str] = None,
        classes: Optional[list] = None,
        pretrained: Optional[bool] = True,
        output_bbox_format: Optional[str] = XYXY,
    ):
        """
        Load a pretrained object detector from MMdetection.

        Parameters
        ----------
        prefix
            The prefix of the MMdetAutoModelForObjectDetection model.
        checkpoint_name
            Name of the mmdet checkpoint.
        classes
            All classes in this dataset.
        pretrained
            Whether using the pretrained mmdet models. If pretrained=True, download the pretrained model.
        """
        super().__init__()
        self.prefix = prefix
        self.pretrained = pretrained
        self.checkpoint = None
        self.checkpoint_name = checkpoint_name
        self.config_file = config_file
        self.classes = classes

        if output_bbox_format.lower() in BBOX_FORMATS:
            self.output_bbox_format = output_bbox_format.lower()
        else:
            raise ValueError(
                f"Not supported bounding box output format for object detection: {output_bbox_format}. All supported bounding box output formats are: {BBOX_FORMATS}."
            )

        # TODO: Config only init (without checkpoint)

        self._get_checkpoint_and_config_file(checkpoint_name=checkpoint_name, config_file=config_file)
        self._load_config()

        self._update_classes(classes)
        self._load_checkpoint(self.checkpoint_file)

    def _reset_classes(self, classes: list):
        temp_ckpt_file = f"temp_ckpt_{int(time.time()*1000)}.pth"
        self._save_weights(temp_ckpt_file)
        self._update_classes(classes)
        self._load_checkpoint()
        os.remove(temp_ckpt_file)

    def _update_classes(self, classes: Optional[list] = None):
        if classes:
            self.num_classes = len(classes)
            self.classes = classes
            update_mmdet_config(key="num_classes", value=self.num_classes, config=self.config)
        else:
            self.num_classes = lookup_mmdet_config(key="num_classes", config=self.config)
            if not self.num_classes:
                raise ValueError("Cannot retrieve num_classes for current model structure.")
            self.classes = None
        self.id2label = dict(zip(range(self.num_classes), range(self.num_classes)))
        return

    def _load_checkpoint(self, checkpoint_file):
        # build model and load pretrained weights
        assert mmdet is not None, 'Please install MMDetection by: pip install "mmdet>=2.28, <3.0.0".'
        self.model = build_detector(self.config.model, test_cfg=self.config.get("test_cfg"))

        if self.pretrained and checkpoint_file is not None:  # TODO: enable training from scratch
            self.checkpoint = load_checkpoint(self.model, checkpoint_file, map_location="cpu")

        # save the config and classes in the model for convenience
        self.model.cfg = self.config
        if self.classes:
            self.model.CLASSES = self.classes
        else:
            if self.checkpoint and "CLASSES" in self.checkpoint.get("meta", {}):
                warnings.simplefilter("once")
                warnings.warn(
                    f"Using classes provided in checkpoints: {self.checkpoint['meta']['CLASSES']}. Provide data while init MultiModalPredictor if this is not expected."
                )
                self.model.CLASSES = self.checkpoint["meta"]["CLASSES"]
            else:
                raise ValueError("Classes need to be specified.")

        self.name_to_id = self.get_layer_ids()
        self.head_layer_names = [n for n, layer_id in self.name_to_id.items() if layer_id <= 0]

    def save(self, save_path: str = "./", tokenizers: Optional[dict] = None):

        weights_save_path = os.path.join(save_path, "model.pth")
        configs_save_path = os.path.join(save_path, "config.py")

        self._save_weights(save_path=weights_save_path)
        self._save_configs(save_path=configs_save_path)

        return save_path

    def _save_weights(self, save_path=None):
        if not save_path:
            save_path = f"./{self.checkpoint_name}_autogluon.pth"

        torch.save({"state_dict": self.model.state_dict(), "meta": {"CLASSES": self.model.CLASSES}}, save_path)

    def _save_configs(self, save_path=None):
        if not save_path:
            save_path = f"./{self.checkpoint_name}_autogluon.py"

        self.config.dump(save_path)

    def _get_checkpoint_and_config_file(self, checkpoint_name: str = None, config_file: str = None):
        from mim.commands import download as mimdownload

        from ..utils import download, get_pretrain_configs_dir

        logger.debug(f"initializing {checkpoint_name}")

        if not checkpoint_name:
            checkpoint_name = self.checkpoint_name
        if not config_file:
            config_file = self.config_file

        mmdet_configs_dir = get_pretrain_configs_dir(subfolder="detection")

        AG_CUSTOM_MODELS = {
            "faster_rcnn_r50_fpn_1x_voc0712": {
                "url": "https://automl-mm-bench.s3.amazonaws.com/voc_script/faster_rcnn_r50_fpn_1x_voc0712_20220320_192712-54bef0f3.pth",
                "config_file": os.path.join(mmdet_configs_dir, "voc", "faster_rcnn_r50_fpn_1x_voc0712.py"),
            },
            "yolox_nano_8x8_300e_coco": {
                "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_nano_8x8_300e_coco.py"),
                "source": "MegVii",
            },
            "yolox_tiny_8x8_300e_coco": {
                "url": "https://download.openmmlab.com/mmdetection/v2.0/yolox/yolox_tiny_8x8_300e_coco/yolox_tiny_8x8_300e_coco_20211124_171234-b4047906.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_tiny_8x8_300e_coco.py"),
            },
            "yolox_s_8x8_300e_coco": {
                "url": "https://download.openmmlab.com/mmdetection/v2.0/yolox/yolox_s_8x8_300e_coco/yolox_s_8x8_300e_coco_20211121_095711-4592a793.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_s_8x8_300e_coco.py"),
            },
            "yolox_m_8x8_300e_coco": {
                "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_m.pth",  # Megvii weight, need more verifications
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_m_8x8_300e_coco.py"),
                "source": "MegVii",
            },
            "yolox_l_8x8_300e_coco": {
                "url": "https://download.openmmlab.com/mmdetection/v2.0/yolox/yolox_l_8x8_300e_coco/yolox_l_8x8_300e_coco_20211126_140236-d3bd2b23.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_l_8x8_300e_coco.py"),
            },
            "yolox_l_objects365": {  # TODO: update with better pretrained weights
                "url": "https://automl-mm-bench.s3.amazonaws.com/object_detection/checkpoints/yolox/yolox_l_objects365_temp.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_l_8x8_300e_coco.py"),
            },
            "yolox_x_8x8_300e_coco": {
                "url": "https://download.openmmlab.com/mmdetection/v2.0/yolox/yolox_x_8x8_300e_coco/yolox_x_8x8_300e_coco_20211126_140254-1ef88d67.pth",
                "config_file": os.path.join(mmdet_configs_dir, "yolox", "yolox_x_8x8_300e_coco.py"),
            },
        }

        if os.path.isfile(checkpoint_name):
            checkpoint_file = checkpoint_name
        elif os.path.isdir(checkpoint_name):
            checkpoint_file = os.path.join(checkpoint_name, "model.pth")
            config_file = os.path.join(checkpoint_name, "config.py")
        else:
            if checkpoint_name in AG_CUSTOM_MODELS:
                # TODO: add sha1_hash
                checkpoint_file = download(
                    url=AG_CUSTOM_MODELS[checkpoint_name]["url"],
                )
                if (
                    "source" in AG_CUSTOM_MODELS[checkpoint_name]
                    and AG_CUSTOM_MODELS[checkpoint_name]["source"] == "MegVii"
                ):
                    checkpoint_file = self.convert_megvii_yolox(checkpoint_file)
            else:
                # download config and checkpoint files using openmim
                checkpoint_file = mimdownload(package="mmdet", configs=[checkpoint_name], dest_root=".")[0]

        if config_file:
            if not os.path.isfile(config_file):
                raise ValueError(f"Invalid checkpoint_name ({checkpoint_name}) or config_file ({config_file}): ")
        else:
            if checkpoint_name in AG_CUSTOM_MODELS:
                config_file = AG_CUSTOM_MODELS[checkpoint_name]["config_file"]
            else:
                try:
                    # download config and checkpoint files using openmim
                    mimdownload(package="mmdet", configs=[checkpoint_name], dest_root=".")
                    config_file = checkpoint_name + ".py"
                except Exception as e:
                    print(e)
                    raise ValueError(f"Invalid checkpoint_name ({checkpoint_name}) or config_file ({config_file}): ")

        self.checkpoint_name = checkpoint_name
        self.checkpoint_file = checkpoint_file
        self.config_file = config_file

    def _load_config(self):
        # read config files
        assert mmcv is not None, "Please install mmcv-full by: mim install mmcv-full."
        if isinstance(self.config_file, str):
            self.config = mmcv.Config.fromfile(self.config_file)
        else:
            if not isinstance(self.config_file, dict):
                raise ValueError(
                    f"The variable config_file has type {type(self.config_file)}."
                    f"Detection Model's config_file should either be a str of file path, or a dict as config."
                )

    @property
    def image_key(self):
        return f"{self.prefix}_{IMAGE}"

    @property
    def image_valid_num_key(self):
        return f"{self.prefix}_{IMAGE_VALID_NUM}"

    @property
    def label_key(self):
        return f"{self.prefix}_{LABEL}"

    @property
    def image_column_prefix(self):
        return f"{self.image_key}_{COLUMN}"

    @property
    def image_feature_dim(self):
        return self.model.num_features

    def forward(
        self,
        batch: dict,
    ):
        """
        Parameters
        ----------
        batch
            A dictionary containing the input mini-batch data.
            We need to use the keys with the model prefix to index required data.

        Returns
        -------
            A dictionary with bounding boxes.
        """
        # TODO: refactor this to work like forward() in MMDet, and support realtime predict
        logger.warning("MMDetAutoModelForObjectDetection.forward() is deprecated since it does not support multi gpu.")

        data = batch[self.image_key]

        data["img_metas"] = [img_metas.data[0] for img_metas in data["img_metas"]]
        data["img"] = [img.data[0] for img in data["img"]]

        device = next(self.model.parameters()).device  # model device
        if next(self.model.parameters()).is_cuda:
            # scatter to specified GPU
            data = scatter(data, [device])[0]
        else:
            for m in self.model.modules():
                assert not isinstance(m, RoIPool), "CPU inference with RoIPool is not supported currently."

        results = self.model(return_loss=False, rescale=True, **data)

        ret = {BBOX: results}
        return {self.prefix: ret}

    def forward_test(self, imgs, img_metas, rescale=True):
        return self.model.forward_test(imgs=imgs, img_metas=img_metas, rescale=rescale)

    def forward_train(self, img, img_metas, gt_bboxes, gt_labels):
        return self.model.forward_train(img=img, img_metas=img_metas, gt_bboxes=gt_bboxes, gt_labels=gt_labels)

    def _parse_losses(self, losses):
        return self.model._parse_losses(losses)

    def get_layer_ids(
        self,
    ):
        """
        Assign an id to each layer. Layer ids will be used in layer-wise lr decay.
        Basically, id gradually increases when going from the output end to
        the input end. The layers defined in this class, e.g., head, have id 0.

        Setting all layers as the same id 0 for now.
        TODO: Need to investigate mmdetection's model definitions
        Currently only head to 0 others to 1.

        Returns
        -------
        A dictionary mapping the layer names (keys) to their ids (values).
        """
        name_to_id = {}
        # for some models, use head lr in "head" of bbox_head
        # now support: yolov3, faster_rcnn, deformable_detr, yolox, vfnet, centernet, cascade_rcnn, detr, htc, atss, ssd
        registered_head_layers_patterns = [
            "bbox_head.fc_cls",
            "bbox_head.fc_reg",
            "bbox_head.convs_pred",
            "bbox_head.cls_branches",
            "bbox_head.multi_level_conv_cls",
            "bbox_head.multi_level_conv_reg",
            "bbox_head.multi_level_conv_obj",
            "bbox_head.vfnet_cls",
            "bbox_head.heatmap_head",
            "bbox_head.atss_cls",
            "bbox_head.cls_convs",
        ]
        # for other models, use head lr in whole bbox_head
        default_head_layers_patterns = ["bbox_head"]

        head_registered = False
        is_yolox = False
        for n, _ in self.named_parameters():
            name_to_id[n] = 1
            for pattern in registered_head_layers_patterns:
                if pattern in n:
                    name_to_id[n] = 0
                    head_registered = True
                if "bbox_head.multi_level_conv_cls" in n:
                    is_yolox = True

        if not head_registered:
            for n, _ in self.named_parameters():
                name_to_id[n] = 1
                for pattern in default_head_layers_patterns:
                    if pattern in n:
                        name_to_id[n] = 0

        if is_yolox and "use_layer_id" in self.config:
            name_to_id = self.get_yolox_layer_ids()

        return name_to_id

    def get_yolox_layer_ids(self):
        # logic not straight forward, need to print out the model to understand
        name_to_value = {}
        for name, _ in self.named_parameters():
            n = name
            n = n.replace("backbone", "0")
            n = n.replace("neck", "1")
            n = n.replace("bbox_head", "2")

            # backbone
            n = n.replace("stem", "0")

            # neck
            n = n.replace("reduce_layers", "0")
            n = n.replace("top_down_blocks", "1")
            n = n.replace("downsamples", "2")
            n = n.replace("bottom_up_blocks", "3")
            n = n.replace("out_convs", "4")

            n = n.replace("main_conv", "0")
            n = n.replace("short_conv", "1")
            n = n.replace("final_conv", "2")
            n = n.replace("blocks", "3")

            # bbox_head
            n = n.replace("multi_level_cls_convs", "0")
            n = n.replace("multi_level_reg_convs", "0")
            n = n.replace("multi_level_conv_cls", "1")
            n = n.replace("multi_level_conv_reg", "1")
            n = n.replace("multi_level_conv_obj", "1")

            value = int("".join(c for c in n if c.isdigit()).ljust(8, "0"))
            name_to_value[name] = value

        values = list(set(name_to_value.values()))
        values.sort(reverse=True)
        value_to_id = dict(zip(values, range(len(values))))

        name_to_id = {}
        for n, _ in self.named_parameters():
            name_to_id[n] = value_to_id[name_to_value[n]]
        return name_to_id

    def convert_megvii_yolox(self, source_path):
        """
        Convert YOLOX in megvii naming to mmdetection naming.
        Using code script from: https://github.com/haiyang-tju/dl_tools/blob/master/megvii_nano_2_mmdet.py
        """
        sd = source_path

        model_dict = torch.load(sd, map_location=torch.device("cpu"))
        if "state_dict" in model_dict:
            model_dict = model_dict["state_dict"]
        if "model" in model_dict:
            model_dict = model_dict["model"]

        new_dict = dict()
        for k, v in model_dict.items():
            new_k = k

            if "backbone.backbone." in k:
                new_k = k.replace("backbone.backbone.", "backbone.")
            if "backbone.dark2." in new_k:
                new_k = new_k.replace("backbone.dark2.", "backbone.stage1.")
            if "backbone.dark3." in new_k:
                new_k = new_k.replace("backbone.dark3.", "backbone.stage2.")
            if "backbone.dark4." in new_k:
                new_k = new_k.replace("backbone.dark4.", "backbone.stage3.")
            if "backbone.dark5." in new_k:
                new_k = new_k.replace("backbone.dark5.", "backbone.stage4.")
            if "dconv." in new_k:
                new_k = new_k.replace("dconv.", "depthwise_conv.")
            if "pconv." in new_k:
                new_k = new_k.replace("pconv.", "pointwise_conv.")
            if "backbone.stage1.1.conv1." in new_k:
                new_k = new_k.replace("backbone.stage1.1.conv1.", "backbone.stage1.1.main_conv.")
            if "backbone.stage1.1.conv2." in new_k:
                new_k = new_k.replace("backbone.stage1.1.conv2.", "backbone.stage1.1.short_conv.")
            if "backbone.stage1.1.conv3." in new_k:
                new_k = new_k.replace("backbone.stage1.1.conv3.", "backbone.stage1.1.final_conv.")
            if ".m." in new_k:
                new_k = new_k.replace(".m.", ".blocks.")
            if "backbone.stage2.1.conv1." in new_k:
                new_k = new_k.replace("backbone.stage2.1.conv1.", "backbone.stage2.1.main_conv.")
            if "backbone.stage2.1.conv2." in new_k:
                new_k = new_k.replace("backbone.stage2.1.conv2.", "backbone.stage2.1.short_conv.")
            if "backbone.stage2.1.conv3." in new_k:
                new_k = new_k.replace("backbone.stage2.1.conv3.", "backbone.stage2.1.final_conv.")
            if "backbone.stage3.1.conv1." in new_k:
                new_k = new_k.replace("backbone.stage3.1.conv1.", "backbone.stage3.1.main_conv.")
            if "backbone.stage3.1.conv2." in new_k:
                new_k = new_k.replace("backbone.stage3.1.conv2.", "backbone.stage3.1.short_conv.")
            if "backbone.stage3.1.conv3." in new_k:
                new_k = new_k.replace("backbone.stage3.1.conv3.", "backbone.stage3.1.final_conv.")
            if "backbone.stage4.2.conv1." in new_k:
                new_k = new_k.replace("backbone.stage4.2.conv1.", "backbone.stage4.2.main_conv.")
            if "backbone.stage4.2.conv2." in new_k:
                new_k = new_k.replace("backbone.stage4.2.conv2.", "backbone.stage4.2.short_conv.")
            if "backbone.stage4.2.conv3." in new_k:
                new_k = new_k.replace("backbone.stage4.2.conv3.", "backbone.stage4.2.final_conv.")
            if "backbone.lateral_conv0." in new_k:
                new_k = new_k.replace("backbone.lateral_conv0.", "neck.reduce_layers.0.")
            if "backbone.reduce_conv1." in new_k:
                new_k = new_k.replace("backbone.reduce_conv1.", "neck.reduce_layers.1.")
            if "backbone.C3_p4." in new_k:
                new_k = new_k.replace("backbone.C3_p4.", "neck.top_down_blocks.0.")
            if "neck.top_down_blocks.0.conv1." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.0.conv1.", "neck.top_down_blocks.0.main_conv.")
            if "neck.top_down_blocks.0.conv2." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.0.conv2.", "neck.top_down_blocks.0.short_conv.")
            if "neck.top_down_blocks.0.conv3." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.0.conv3.", "neck.top_down_blocks.0.final_conv.")
            if "backbone.C3_p3." in new_k:
                new_k = new_k.replace("backbone.C3_p3.", "neck.top_down_blocks.1.")
            if "neck.top_down_blocks.1.conv1." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.1.conv1.", "neck.top_down_blocks.1.main_conv.")
            if "neck.top_down_blocks.1.conv2." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.1.conv2.", "neck.top_down_blocks.1.short_conv.")
            if "neck.top_down_blocks.1.conv3." in new_k:
                new_k = new_k.replace("neck.top_down_blocks.1.conv3.", "neck.top_down_blocks.1.final_conv.")

            if "backbone.bu_conv2." in new_k:
                new_k = new_k.replace("backbone.bu_conv2.", "neck.downsamples.0.")
            if "backbone.bu_conv1." in new_k:
                new_k = new_k.replace("backbone.bu_conv1.", "neck.downsamples.1.")

            if "backbone.C3_n3." in new_k:
                new_k = new_k.replace("backbone.C3_n3.", "neck.bottom_up_blocks.0.")
            if "neck.bottom_up_blocks.0.conv1." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.0.conv1.", "neck.bottom_up_blocks.0.main_conv.")
            if "neck.bottom_up_blocks.0.conv2." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.0.conv2.", "neck.bottom_up_blocks.0.short_conv.")
            if "neck.bottom_up_blocks.0.conv3." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.0.conv3.", "neck.bottom_up_blocks.0.final_conv.")
            if "backbone.C3_n4." in new_k:
                new_k = new_k.replace("backbone.C3_n4.", "neck.bottom_up_blocks.1.")
            if "neck.bottom_up_blocks.1.conv1." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.1.conv1.", "neck.bottom_up_blocks.1.main_conv.")
            if "neck.bottom_up_blocks.1.conv2." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.1.conv2.", "neck.bottom_up_blocks.1.short_conv.")
            if "neck.bottom_up_blocks.1.conv3." in new_k:
                new_k = new_k.replace("neck.bottom_up_blocks.1.conv3.", "neck.bottom_up_blocks.1.final_conv.")

            if "head.stems." in new_k:
                new_k = new_k.replace("head.stems.", "neck.out_convs.")
            if "head.cls_convs." in new_k:
                new_k = new_k.replace("head.cls_convs.", "bbox_head.multi_level_cls_convs.")
            if "head.reg_convs." in new_k:
                new_k = new_k.replace("head.reg_convs.", "bbox_head.multi_level_reg_convs.")
            if "head.cls_preds." in new_k:
                new_k = new_k.replace("head.cls_preds.", "bbox_head.multi_level_conv_cls.")
            if "head.reg_preds." in new_k:
                new_k = new_k.replace("head.reg_preds.", "bbox_head.multi_level_conv_reg.")
            if "head.obj_preds." in new_k:
                new_k = new_k.replace("head.obj_preds.", "bbox_head.multi_level_conv_obj.")

            if "bbox_head.multi_level_conv_cls." in new_k:
                if self.classes:
                    new_dict[new_k] = v[: len(self.classes), ...]  # there take the num_classes
                else:
                    new_dict[new_k] = v
            else:
                new_dict[new_k] = v

        data = {"state_dict": new_dict}

        target_directory = os.path.splitext(sd)[0] + f"_cvt.pth"
        torch.save(data, target_directory)

        return target_directory
