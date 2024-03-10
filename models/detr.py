# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
DETR model and criterion classes.
"""
import copy
import math
import torch
import torch.nn.functional as F
from torch import nn

from torchvision.ops import batched_nms
from util import box_ops
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized)

from .backbone import build_backbone
from .matcher import build_matcher
from .segmentation import (DETRsegm, PostProcessPanoptic, PostProcessSegm,
                           dice_loss, sigmoid_focal_loss)
from .transformer import build_transformer, MLP
import json
import os

def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

def transform_tensor_to_list(l):
    return l.cpu().tolist()

def transform_tensors_to_list(l):
    if torch.is_tensor(l):
        return transform_tensor_to_list(l)
    if isinstance(l, list):
        r = []
        for i in l:
            r.append(transform_tensors_to_list(i))
        return r
    if isinstance(l, dict):
        r = {}
        for k,v in l.items():
            r[k] = transform_tensors_to_list(v)
        return r
    return l

def create_folder_if_not_exists(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

class DETR(nn.Module):
    """ This is the DETR module that performs object detection """
    def __init__(self, backbone, transformer, num_classes, num_queries, aux_loss=False,
                 box_refine=False, num_feature_levels=3, init_ref_dim=2):
        """ Initializes the model.
        Parameters:
            backbone: torch module of the backbone to be used. See backbone.py
            transformer: torch module of the transformer architecture. See transformer.py
            num_classes: number of object classes
            num_queries: number of object queries, ie detection slot. This is the maximal number of objects
                         DETR can detect in a single image. For COCO, we recommend 100 queries.
            aux_loss: True if auxiliary decoding losses (loss at each decoder layer) are to be used.
        """
        super().__init__()
        self.num_queries = num_queries
        self.transformer = transformer
        hidden_dim = transformer.d_model
        self.class_embed = nn.Linear(hidden_dim, num_classes + 1)
        self.bbox_embed = MLP(hidden_dim, hidden_dim, 4, 3)
        self.query_embed = nn.Embedding(num_queries, hidden_dim)
        # self.input_proj = nn.Conv2d(backbone.num_channels, hidden_dim, kernel_size=1)
        self.num_feature_levels = num_feature_levels

        if num_feature_levels > 1:
            num_backbone_outs = len(backbone.strides)
            input_proj_list = []
            for _ in range(num_backbone_outs):
                in_channels = backbone.num_channels[_]
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
            # the 3x3 conv with large input channels takes lots of params
            for _ in range(num_feature_levels - num_backbone_outs):
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=3, stride=2, padding=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
                in_channels = hidden_dim
            self.input_proj = nn.ModuleList(input_proj_list)
        else:
            self.input_proj = nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(backbone.num_channels[0], hidden_dim, kernel_size=1),
                )])

        self.backbone = backbone
        self.aux_loss = aux_loss
        self.box_refine = box_refine

        prior_prob = 0.01
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        self.class_embed.bias.data = torch.ones(num_classes + 1) * bias_value
        nn.init.constant_(self.bbox_embed.layers[-1].weight.data, 0)
        nn.init.constant_(self.bbox_embed.layers[-1].bias.data, 0)

        num_pred = transformer.decoder.num_layers
        if box_refine:
            self.class_embed = _get_clones(self.class_embed, num_pred)
            self.bbox_embed = _get_clones(self.bbox_embed, num_pred)
            nn.init.constant_(self.bbox_embed[0].layers[-1].bias.data[2:], -2.0)
        else:
            nn.init.constant_(self.bbox_embed.layers[-1].bias.data[2:], -2.0)
            self.class_embed = nn.ModuleList([self.class_embed for _ in range(num_pred)])
            self.bbox_embed = nn.ModuleList([self.bbox_embed for _ in range(num_pred)])
        # Note in our impl, decoder.bbox_embed is never None, for guiding the RoI extraction
        self.transformer.decoder.bbox_embed = self.bbox_embed

        # make predictions relative to init references
        self.ref_point_head = MLP(hidden_dim, hidden_dim, output_dim=init_ref_dim, num_layers=2)
        self.transformer.decoder.ref_point_head = self.ref_point_head
        if box_refine:
            self.transformer.decoder.box_refine = self.box_refine
        self._cnt = 0

    def store_results(self, hidden_states, outs, store_path = "DEDETR_CITY", split = "val"):
        batch_size = hidden_states.shape[1]
        for i in range(batch_size):
            json_data = {}
            json_data['feature'] = transform_tensors_to_list(hidden_states[-1,i])
            json_data['pred_logits'] = transform_tensors_to_list(outs['pred_logits'][i])
            json_data['pred_boxes'] = transform_tensors_to_list(outs['pred_boxes'][i])

            path = "./pro_data/" + store_path
            create_folder_if_not_exists(path)
            path = os.path.join(path, split)
            create_folder_if_not_exists(path)
            path = os.path.join(path, "outputs")
            create_folder_if_not_exists(path)
            path =  os.path.join(path, str(self._cnt) +".json")
            with open(path, "w") as outfile:
                json.dump(json_data, outfile)

            self._cnt += 1
        
    def forward(self, samples: NestedTensor, meta_info):
        """ The forward expects a NestedTensor, which consists of:
               - samples.tensor: batched images, of shape [batch_size x 3 x H x W]
               - samples.mask: a binary mask of shape [batch_size x H x W], containing 1 on padded pixels

            It returns a dict with the following elements:
               - "pred_logits": the classification logits (including no-object) for all queries.
                                Shape= [batch_size x num_queries x (num_classes + 1)]
               - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                               (center_x, center_y, height, width). These values are normalized in [0, 1],
                               relative to the size of each individual image (disregarding possible padding).
                               See PostProcess for information on how to retrieve the unnormalized bounding box.
               - "aux_outputs": Optional, only returned when auxilary losses are activated. It is a list of
                                dictionnaries containing the two above keys for each decoder layer.
        """
        if isinstance(samples, (list, torch.Tensor)):
            samples = nested_tensor_from_tensor_list(samples)
        features, pos = self.backbone(samples)

        # modified from deformable detr
        srcs = []
        masks = []
        for l, feat in enumerate(features):
            src, mask = feat.decompose()
            srcs.append(self.input_proj[l](src))
            masks.append(mask)
            assert mask is not None
        if self.num_feature_levels > len(srcs):
            _len_srcs = len(srcs)
            for l in range(_len_srcs, self.num_feature_levels):
                if l == _len_srcs:
                    src = self.input_proj[l](features[-1].tensors)
                else:
                    src = self.input_proj[l](srcs[-1])
                m = samples.mask
                mask = F.interpolate(m[None].float(), size=src.shape[-2:]).to(torch.bool)[0]
                pos_l = self.backbone[1](NestedTensor(src, mask)).to(src.dtype)
                srcs.append(src)
                masks.append(mask)
                pos.append(pos_l)

        ms_feats = [torch.cat([src_, pos_], dim=1) for src_, pos_ in zip(srcs, pos)]  # (bs, 2*c, H, W)

        assert len(ms_feats) == self.num_feature_levels
        if self.num_feature_levels == 1:
            ms_feats = None
            srcs, masks, pos = srcs[0], masks[0], pos[0]
        elif self.num_feature_levels in [3, 4]:  # only the /32 scale is processed by the encoder
            srcs, masks, pos = srcs[2], masks[2], pos[2]
        else:
            raise NotImplementedError

        hs, memory, outputs_coord = self.transformer(
            srcs, masks, self.query_embed.weight, pos, meta_info=meta_info, ms_feats=ms_feats,
        )

        outputs_classes = []
        for lvl in range(hs.shape[0]):
            outputs_class = self.class_embed[lvl](hs[lvl])
            outputs_classes.append(outputs_class)
        outputs_class = torch.stack(outputs_classes)

        out = {'pred_logits': outputs_class[-1], 'pred_boxes': outputs_coord[-1]}
        if self.aux_loss:
            out['aux_outputs'] = self._set_aux_loss(outputs_class, outputs_coord)
        self.store_results(hs, out, store_path = "DETR_CITY", split = "train")
        return out

    @torch.jit.unused
    def _set_aux_loss(self, outputs_class, outputs_coord):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b}
                for a, b in zip(outputs_class[:-1], outputs_coord[:-1])]


class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, num_classes, matcher, weight_dict, eos_coef, losses,
                 repeat_label=None, repeat_ratio=None):
        """ Create the criterion.
        Parameters:
            num_classes: number of object categories, omitting the special no-object category
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            eos_coef: relative classification weight applied to the no-object category
            losses: list of all the losses to be applied. See get_loss for list of available losses.
        """
        super().__init__()
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.eos_coef = eos_coef
        self.losses = losses
        empty_weight = torch.ones(self.num_classes + 1)
        empty_weight[-1] = self.eos_coef
        self.register_buffer('empty_weight', empty_weight)
        assert repeat_label is None or repeat_ratio is None, "during init, only one of them shall be set"
        self.repeat_label = repeat_label
        self.repeat_ratio = repeat_ratio
        self._cnt = 0

    def loss_labels(self, outputs, targets, indices, num_boxes, log=True):
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        src_logits = outputs['pred_logits']

        idx = self._get_src_permutation_idx(indices)
        target_classes_o = torch.cat([t["labels"][J] for t, (_, J) in zip(targets, indices)])
        target_classes = torch.full(src_logits.shape[:2], self.num_classes,
                                    dtype=torch.int64, device=src_logits.device)
        target_classes[idx] = target_classes_o

        loss_ce = F.cross_entropy(src_logits.transpose(1, 2), target_classes, self.empty_weight)
        losses = {'loss_ce': loss_ce}

        if log:
            # TODO this should probably be a separate loss, not hacked in this one here
            losses['class_error'] = 100 - accuracy(src_logits[idx], target_classes_o)[0]
        return losses

    @torch.no_grad()
    def loss_cardinality(self, outputs, targets, indices, num_boxes):
        """ Compute the cardinality error, ie the absolute error in the number of predicted non-empty boxes
        This is not really a loss, it is intended for logging purposes only. It doesn't propagate gradients
        """
        pred_logits = outputs['pred_logits']
        device = pred_logits.device
        tgt_lengths = torch.as_tensor([len(v["labels"]) for v in targets], device=device)
        # Count the number of predictions that are NOT "no-object" (which is the last class)
        card_pred = (pred_logits.argmax(-1) != pred_logits.shape[-1] - 1).sum(1)
        card_err = F.l1_loss(card_pred.float(), tgt_lengths.float())
        losses = {'cardinality_error': card_err}
        return losses

    def loss_boxes(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, w, h), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        loss_bbox = F.l1_loss(src_boxes, target_boxes, reduction='none')

        losses = {}
        losses['loss_bbox'] = loss_bbox.sum() / num_boxes

        loss_giou = 1 - torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes),
            box_ops.box_cxcywh_to_xyxy(target_boxes)))
        losses['loss_giou'] = loss_giou.sum() / num_boxes
        return losses

    def loss_masks(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        src_idx = self._get_src_permutation_idx(indices)
        tgt_idx = self._get_tgt_permutation_idx(indices)
        src_masks = outputs["pred_masks"]
        src_masks = src_masks[src_idx]
        masks = [t["masks"] for t in targets]
        # TODO use valid to mask invalid areas due to padding in loss
        target_masks, valid = nested_tensor_from_tensor_list(masks).decompose()
        target_masks = target_masks.to(src_masks)
        target_masks = target_masks[tgt_idx]

        # upsample predictions to the target size
        src_masks = interpolate(src_masks[:, None], size=target_masks.shape[-2:],
                                mode="bilinear", align_corners=False)
        src_masks = src_masks[:, 0].flatten(1)

        target_masks = target_masks.flatten(1)
        target_masks = target_masks.view(src_masks.shape)
        losses = {
            "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes),
            "loss_dice": dice_loss(src_masks, target_masks, num_boxes),
        }
        return losses

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets, indices, num_boxes, **kwargs):
        loss_map = {
            'labels': self.loss_labels,
            'cardinality': self.loss_cardinality,
            'boxes': self.loss_boxes,
            'masks': self.loss_masks
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, indices, num_boxes, **kwargs)

    def store_results(self, targets, store_path = "DEDETR_CITY", split = "val"):
        batch_size = len(targets)
        for i in range(batch_size):
            path = os.path.join("./pro_data/" + store_path, split, "outputs", str(self._cnt) +".json")
            with open(path, 'r') as f:
                json_data = json.load(f)
            
            json_data['gt_labels'] = transform_tensors_to_list(targets[i]['labels'])
            json_data['gt_boxes'] = transform_tensors_to_list(targets[i]['boxes'])
            json_data['orig_size'] = transform_tensors_to_list(targets[i]['orig_size'])
            json_data['size'] = transform_tensors_to_list(targets[i]['size'])
            
            with open(path, "w") as outfile:
                json.dump(json_data, outfile)

            self._cnt += 1
    
    def forward(self, outputs, targets):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        self.store_results(targets, store_path = "DETR_CITY", split = "train")
        bs, num_queries, _ = outputs['pred_logits'].shape
        if self.training:
            if self.repeat_label is not None or self.repeat_ratio is not None:
                num_fore = int(self.repeat_ratio * num_queries) if self.repeat_ratio is not None else None
                # keys in targets: 'boxes', 'labels', 'area', 'iscrowd', 'orig_size', 'size', 'image_id'
                for batch_idx in range(len(targets)):
                    num_inst = len(targets[batch_idx]['labels'])
                    if self.repeat_ratio is not None and (num_inst == 0 or num_inst >= num_fore):
                        continue
                    repeat_time = num_fore // num_inst if num_fore is not None else self.repeat_label
                    repeat_rand = num_fore % num_inst if num_fore is not None else None
                    targets[batch_idx]['boxes'] = targets[batch_idx]['boxes'].repeat(repeat_time, 1)
                    targets[batch_idx]['labels'] = targets[batch_idx]['labels'].repeat(repeat_time)
                    targets[batch_idx]['area'] = targets[batch_idx]['area'].repeat(repeat_time)
                    targets[batch_idx]['iscrowd'] = targets[batch_idx]['iscrowd'].repeat(repeat_time)
                    if repeat_rand is not None:
                        sample_idx = torch.randperm(num_inst, device=targets[batch_idx]['boxes'].device)[:repeat_rand]
                        targets[batch_idx]['boxes'] = torch.cat(
                            [targets[batch_idx]['boxes'], targets[batch_idx]['boxes'][sample_idx]], dim=0)
                        targets[batch_idx]['labels'] = torch.cat(
                            [targets[batch_idx]['labels'], targets[batch_idx]['labels'][sample_idx]], dim=0)
                        targets[batch_idx]['area'] = torch.cat(
                            [targets[batch_idx]['area'], targets[batch_idx]['area'][sample_idx]], dim=0)
                        targets[batch_idx]['iscrowd'] = torch.cat(
                            [targets[batch_idx]['iscrowd'], targets[batch_idx]['iscrowd'][sample_idx]], dim=0)
                    # create repeat label. Note 0 for repeated while 1 for not repeated
                    targets[batch_idx]['repeat'] = torch.zeros(num_inst * repeat_time, dtype=torch.int64,
                                                               device=targets[batch_idx]['labels'].device)
                    targets[batch_idx]['repeat'][:num_inst] = 1

        outputs_without_aux = {k: v for k, v in outputs.items() if k != 'aux_outputs'}

        # Retrieve the matching between the outputs of the last layer and the targets
        indices = self.matcher(outputs_without_aux, targets)

        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["labels"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()

        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            losses.update(self.get_loss(loss, outputs, targets, indices, num_boxes))

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                indices = self.matcher(aux_outputs, targets)
                for loss in self.losses:
                    if loss == 'masks':
                        # Intermediate masks losses are too costly to compute, we ignore them.
                        continue
                    kwargs = {}
                    if loss == 'labels':
                        # Logging is enabled only for the last layer
                        kwargs = {'log': False}
                    l_dict = self.get_loss(loss, aux_outputs, targets, indices, num_boxes, **kwargs)
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)

        return losses


class PostProcess(nn.Module):
    """ This module converts the model's output into the format expected by the coco api"""
    def __init__(self, num_queries=100, nms=False, nms_thresh=0.7, nms_remove=0.01):
        super().__init__()
        self.num_queries = num_queries
        self.nms = nms
        self.nms_thresh = nms_thresh
        self.nms_remove = nms_remove

    @torch.no_grad()
    def forward(self, outputs, target_sizes):
        """ Perform the computation
        Parameters:
            outputs: raw outputs of the model
            target_sizes: tensor of dimension [batch_size x 2] containing the size of each images of the batch
                          For evaluation, this must be the original image size (before any data augmentation)
                          For visualization, this should be the image size after data augment, but before padding
        """
        out_logits, out_bbox = outputs['pred_logits'], outputs['pred_boxes']

        assert len(out_logits) == len(target_sizes)
        assert target_sizes.shape[1] == 2

        prob = F.softmax(out_logits, -1)
        scores, labels = prob[..., :-1].max(-1)

        # convert to [x0, y0, x1, y1] format
        boxes = box_ops.box_cxcywh_to_xyxy(out_bbox)

        if self.nms:
            # perform nms here
            batch_keep = []
            for (bbox_, score_, label_) in zip(boxes, scores, labels):
                keep_ = batched_nms(bbox_, score_, label_, iou_threshold=self.nms_thresh)

                # concate the eliminated preds
                full_index = torch.ones(self.num_queries, device=keep_.device)
                full_index[keep_] = 0
                non_keep_ = full_index.nonzero(as_tuple=True)[0]
                # use *0.01 to mimic remove dets, and also to keep 100 dets.
                score_[non_keep_] *= self.nms_remove

                keep_ = torch.cat([keep_, non_keep_], dim=0)
                keep_ = keep_[:100].view(1, -1)
                batch_keep.append(keep_)
            topk_indexes = torch.cat(batch_keep, dim=0)  # (bs, 100)

            labels = torch.gather(labels, 1, topk_indexes)
            scores = torch.gather(scores, 1, topk_indexes)
            boxes = torch.gather(boxes, 1, topk_indexes.unsqueeze(-1).repeat(1, 1, 4))

        # and from relative [0, 1] to absolute [0, height] coordinates
        img_h, img_w = target_sizes.unbind(1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)
        boxes = boxes * scale_fct[:, None, :]

        results = [{'scores': s, 'labels': l, 'boxes': b} for s, l, b in zip(scores, labels, boxes)]

        return results


def build(args):
    # the `num_classes` naming here is somewhat misleading.
    # it indeed corresponds to `max_obj_id + 1`, where max_obj_id
    # is the maximum id for a class in your dataset. For example,
    # COCO has a max_obj_id of 90, so we pass `num_classes` to be 91.
    # As another example, for a dataset that has a single class with id 1,
    # you should pass `num_classes` to be 2 (max_obj_id + 1).
    # For more details on this, check the following discussion
    # https://github.com/facebookresearch/detr/issues/108#issuecomment-650269223

    # num_classes = 20 if args.dataset_file != 'coco' else 91
    # if args.dataset_file == "coco_panoptic":
    #     # for panoptic, we just add a num_classes that is large enough to hold
    #     # max_obj_id + 1, but the exact value doesn't really matter
    #     num_classes = 250
    dataset2numcls = {
        'coco': 91, 'coco_panoptic': 250,
        'cityscapes': 9, 'voc': 20,
    }
    if args.dataset_file in dataset2numcls.keys():
        num_classes = dataset2numcls[args.dataset_file]
    elif 'coco' in args.dataset_file:
        num_classes = dataset2numcls['coco']  # for sub-sampled coco dataset
    else:
        raise NotImplementedError("unsupported dataset {}".format(args.dataset_file))
    device = torch.device(args.device)

    backbone = build_backbone(args)

    transformer = build_transformer(args)

    model = DETR(
        backbone,
        transformer,
        num_classes=num_classes,
        num_queries=args.num_queries,
        aux_loss=args.aux_loss,
        box_refine=args.box_refine,
        num_feature_levels=args.num_feature_levels,
        init_ref_dim=args.init_ref_dim,
    )
    if args.masks:
        model = DETRsegm(model, freeze_detr=(args.frozen_weights is not None))
    matcher = build_matcher(args)
    weight_dict = {'loss_ce': 1, 'loss_bbox': args.bbox_loss_coef}
    weight_dict['loss_giou'] = args.giou_loss_coef
    if args.masks:
        weight_dict["loss_mask"] = args.mask_loss_coef
        weight_dict["loss_dice"] = args.dice_loss_coef
    # TODO this is a hack
    if args.aux_loss:
        aux_weight_dict = {}
        for i in range(args.dec_layers - 1):
            aux_weight_dict.update({k + f'_{i}': v for k, v in weight_dict.items()})
        weight_dict.update(aux_weight_dict)

    losses = ['labels', 'boxes', 'cardinality']
    if args.masks:
        losses += ["masks"]
    criterion = SetCriterion(num_classes, matcher=matcher, weight_dict=weight_dict,
                             eos_coef=args.eos_coef, losses=losses,
                             repeat_label=args.repeat_label, repeat_ratio=args.repeat_ratio)
    criterion.to(device)
    postprocessors = {'bbox': PostProcess(num_queries=args.num_queries, nms=args.nms, nms_thresh=args.nms_thresh,
                                          nms_remove=args.nms_remove)}
    if args.masks:
        postprocessors['segm'] = PostProcessSegm()
        if args.dataset_file == "coco_panoptic":
            is_thing_map = {i: i <= 90 for i in range(201)}
            postprocessors["panoptic"] = PostProcessPanoptic(is_thing_map, threshold=0.85)

    return model, criterion, postprocessors
