import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import math
import numpy as np
from typing import Union

from einops import rearrange
from einops.layers.torch import Rearrange
from collections import OrderedDict

# from timm.models.layers import DropPath, to_2tuple, trunc_normal_
# =====================================================================================
# Below functions are from timm.models.layers import DropPath, to_2tuple, trunc_normal_
# =====================================================================================
from itertools import repeat
import collections.abc
import warnings


def drop_path(x, drop_prob: float = 0., training: bool = False):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).

    This is the same as the DropConnect impl I created for EfficientNet, etc networks, however,
    the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for
    changing the layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use
    'survival rate' as the argument.

    """
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()  # binarize
    output = x.div(keep_prob) * random_tensor
    return output


class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)

# From PyTorch internals
def _ntuple(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable):
            return x
        return tuple(repeat(x, n))
    return parse


to_1tuple = _ntuple(1)
to_2tuple = _ntuple(2)
to_3tuple = _ntuple(3)
to_4tuple = _ntuple(4)
to_ntuple = _ntuple


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Args:
        tensor: an n-dimensional `torch.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = torch.empty(3, 5)
        >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

# =====================================================================================
# Above functions are from timm.models.layers import DropPath, to_2tuple, trunc_normal_
# =====================================================================================
class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

class MixerMlp(Mlp):
    def forward(self, x):
        return super().forward(x.transpose(1, 2)).transpose(1, 2)


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)

class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)

class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, drop_path: float = 0.):
        super().__init__()
        self.n_head = n_head
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

    def attention(self, x: torch.Tensor, attn_mask=None):
        if attn_mask is not None:
            if hasattr(attn_mask, '__call__'):
                attn_mask_ = attn_mask(x.size()[0])   # LND
            else:
                ext_attn_mask = attn_mask.unsqueeze(1)
                ext_attn_mask = (1.0 - ext_attn_mask) * -1000000.0
                ext_attn_mask = ext_attn_mask.expand(-1, attn_mask.size(1), -1)
                attn_mask_ = ext_attn_mask.repeat_interleave(self.n_head, dim=0)
        else:
            attn_mask_ = None

        attn_mask_ = attn_mask_.to(dtype=x.dtype, device=x.device) if attn_mask_ is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=attn_mask_)[0]

    def forward(self, x: torch.Tensor, attn_mask=None, video_frame=-1):
        x = x.permute(1, 0, 2)  # x: LND
        x = x + self.drop_path(self.attention(self.ln_1(x), attn_mask=attn_mask))
        x = x + self.drop_path(self.mlp(self.ln_2(x)))
        x = x.permute(1, 0, 2)  # x: NLD
        return x


class CrossAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int):
        super().__init__()
        self.n_head = n_head
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_x = LayerNorm(d_model)
        self.ln_k = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)

    def forward(self, q: torch.Tensor, k: torch.Tensor):
        q = q.permute(1, 0, 2)  # x: LND
        q = q + self.attn(self.ln_x(q), self.ln_k(k), self.ln_k(k), need_weights=False,)[0]
        q = q + self.mlp(self.ln_2(q))
        q = q.permute(1, 0, 2)  # x: NLD
        return q


def gumbel_softmax(logits: torch.Tensor, tau: float = 1, hard: bool = False, dim: int = -1, is_training=True) -> torch.Tensor:
    if is_training:
        gumbel_dist = torch.distributions.gumbel.Gumbel(
            torch.tensor(0., device=logits.device, dtype=logits.dtype),
            torch.tensor(1., device=logits.device, dtype=logits.dtype))
        gumbels = gumbel_dist.sample(logits.shape)

        gumbels = (logits + gumbels) / tau  # ~Gumbel(logits,tau)
        y_soft = gumbels.softmax(dim)
    else:
        y_soft = logits.softmax(dim)

    if hard:
        # Straight through.
        index = y_soft.max(dim, keepdim=True)[1]
        y_hard = torch.zeros_like(logits, memory_format=torch.legacy_contiguous_format).scatter_(dim, index, 1.0)
        ret = y_hard - y_soft.detach() + y_soft
    else:
        # Reparametrization trick.
        ret = y_soft

    return ret

class SemanticLearnerModule(nn.Module):
    def __init__(self, in_channels, num_tokens, num_heads, cross_layer=1):
        """Applies learnable tokenization to the 2D inputs.
        Args:
          inputs: Inputs of shape `[bs, h, w, c]`.
        Returns:
          Output of shape `[bs, n_token, c]`.
        """
        super(SemanticLearnerModule, self).__init__()

        self.in_channels = in_channels
        self.num_heads = num_heads # in_channels must both be divisible by groups
        self.norm = nn.LayerNorm(self.in_channels)  # Operates on the last axis (c) of the input data.

        self.semantic_center = Parameter(torch.Tensor(*[num_tokens, in_channels]))
        trunc_normal_(self.semantic_center, std=.02)

        self.cross_att = nn.Sequential(OrderedDict(
            [(str(indx), CrossAttentionBlock(self.in_channels, n_head=self.num_heads)) for indx in range(cross_layer)]
        ))
        self.cross_ln = nn.LayerNorm(self.in_channels)

        self.k_conv = nn.Conv1d(self.in_channels, self.in_channels, kernel_size=(1,), stride=(1,), padding=(0,), groups=self.num_heads, bias=False)
        self.k_ln = nn.LayerNorm(self.in_channels)

        self.v_conv = nn.Conv1d(self.in_channels, self.in_channels, kernel_size=(1,), stride=(1,), padding=(0,), groups=self.num_heads, bias=False)

        self.proj_o = nn.Sequential(OrderedDict([
            ('ln', nn.LayerNorm(self.in_channels)),
            ('mlp', Mlp(self.in_channels, 4 * self.in_channels, self.in_channels)),
            ('act', QuickGELU())
        ]))

    def forward(self, inputs: torch.Tensor):
        """
        :param inputs: (B, L, H)
        :param attn_mask:
        :param video_frame:
        :return:
        """
        bs, l, c = inputs.size()
        hw_ = int(np.sqrt(l))

        org_inputs = inputs
        in_feature = self.norm(inputs).permute(0, 2, 1).contiguous()   # (B, H, L)

        q_feat = self.semantic_center.to(device=org_inputs.device, dtype=org_inputs.dtype)
        q_feat = q_feat.unsqueeze(0).repeat(bs, 1, 1)   # [bs, n_token, c]

        for layer_id_, attn_fct_ in enumerate(self.cross_att):
            kv_ = torch.cat([q_feat, org_inputs], dim=1)
            q_feat = attn_fct_(q_feat, kv_)

        q_feat = self.cross_ln(q_feat).to(dtype=in_feature.dtype)  # [bs, n_token, c]

        k_feat = self.k_conv(in_feature).permute(0, 2, 1).contiguous()  # (B, L, H)
        k_feat = self.k_ln(k_feat).to(dtype=in_feature.dtype)     # Shape:  [bs, h*w, c]

        v_feat = self.v_conv(in_feature).permute(0, 2, 1).contiguous().to(dtype=in_feature.dtype)  # (B, L, H)

        attn = torch.einsum("...si,...di->...sd",  q_feat, k_feat)  # [bs, n_token, h*w]
        hard_attn = gumbel_softmax(attn, tau=0.9, hard=True, dim=1, is_training=self.training)
        soft_attn = F.softmax(attn, dim=1)

        # Produced the attended inputs.
        outputs = torch.einsum("...si,...id->...sd",  hard_attn, v_feat)  # (B, n_token, c)
        outputs = outputs / torch.clamp_min(torch.sum(hard_attn, dim=-1).unsqueeze(-1), min=1.0)

        outputs = self.proj_o(q_feat + outputs)  # (B, n_token, c)

        return outputs, hard_attn, soft_attn, q_feat

class ReconstructLayer(nn.Module):
    def __init__(self, in_channels, out_channels,):
        """
        Args:
        Returns:
        """
        super(ReconstructLayer, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.rec_proj_a = nn.Sequential(OrderedDict([
            ("a_fc", nn.Linear(self.in_channels, self.in_channels)),
        ]))
        self.proj_o = nn.Sequential(OrderedDict([
            ('act_a', QuickGELU()),
        ]))

    def forward(self, inputs: torch.Tensor, attn: torch.Tensor):
        """
        :param inputs: (B, L, H)
        :param attn: (B, L, M)
        :return: (B, M, H)
        """
        attn = attn.permute(0, 2, 1).contiguous()
        attn = self.rec_proj_a(attn)  # (B, M, L)
        attn = attn.to(dtype=inputs.dtype)
        outputs = torch.einsum("...dh,...md->...mh",  inputs, attn)  # (B, M, H)
        outputs = self.proj_o(outputs)

        return outputs


class SegViT(nn.Module):
    def __init__(self, dim_in, patch_size=32, input_resolution=224, first_stage_layer=10, cross_layer=2, group_num=8):

        super().__init__()
        self.dim_in = dim_in

        self.patch_len = input_resolution // patch_size

        depths = [first_stage_layer, 12-first_stage_layer]   # default: [10, 2]
        layer_dims = [dim_in, dim_in]
        num_heads = [itm_ // 64 for itm_ in layer_dims]

        # === Layer part0 ===
        i_layer = 0
        self.layers0 = nn.Sequential(
            OrderedDict(
                [(str(indx), ResidualAttentionBlock(layer_dims[i_layer], num_heads[i_layer])) for indx in range(depths[i_layer])]
            )
        )

        self.semantic_layer2 = SemanticLearnerModule(in_channels=layer_dims[i_layer], num_tokens=group_num,
                                                     num_heads=num_heads[i_layer], cross_layer=cross_layer)

        # === Layer part1 ===
        i_layer = 1
        if depths[i_layer] > 0:
            self.layers2 = nn.Sequential(
                OrderedDict(
                    [(str(indx), ResidualAttentionBlock(layer_dims[i_layer], num_heads[i_layer])) for indx in range(depths[i_layer])]
                )
            )
            # For MAE mask
            self.layers_mae2 = nn.Sequential(
                OrderedDict(
                    [(str(indx), ResidualAttentionBlock(layer_dims[i_layer], num_heads[i_layer])) for indx in range(depths[i_layer])]
                )
            )
        else:
            self.layers2 = nn.Identity()
            self.layers_mae2 = nn.Identity()

        self.reconstruct_layer2 = ReconstructLayer(in_channels=group_num, out_channels=layer_dims[i_layer])

        self.apply(self._init_weights)


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x: torch.Tensor, attn_mask=None, video_frame=-1):
        """ :parameters are compatible to CLIP
        :param x: LND
        :param attn_mask: 1: removed, 0: keeped
        :param video_frame:
        """

        mid_states = {}
        mid_states['hidden'] = None
        mid_states['attns'] = []

        x = x.permute(1, 0, 2)   # x: NLD

        if attn_mask is not None: raise NotImplementedError

        # split [CLS]
        cls, x_ = torch.split(x, [1, x.size(1)-1], dim=1)

        x_ = self.layers0(x_)

        if self.patch_len ** 2 != x_.size(1) and 4 * (self.patch_len ** 2) != x_.size(1):    # if do MAE mask

            # =====reconstruct from semantic tokens=======
            sx_, hard_attn_2, soft_attn_2, _ = self.semantic_layer2(x_)
            x_ = self.reconstruct_layer2(sx_, hard_attn_2)  # s2->s1/m
            x_ = self.layers_mae2(x_)

            # ============================================
            mid_states['hidden'] = x_
            cls = torch.mean(x_, dim=1, keepdim=True)
            x = torch.cat([cls, x_], dim=1)
        else:
            mid_states['hidden'] = x_
            hard_attn_1, soft_attn_1 = None, None

            x_, hard_attn_2, soft_attn_2, _ = self.semantic_layer2(x_)
            x_ = self.layers2(x_)

            cls = torch.max(x_, dim=1, keepdim=True)[0]
            x = torch.cat([cls, x_], dim=1)

            # semantic_attn is always the last one, (B, n_token, w*h)
            if hard_attn_1 is not None:
                mid_states['attns'].append({"soft_attn": soft_attn_1, "hard_attn": hard_attn_1})

            mid_states['attns'].append({"soft_attn": soft_attn_2, "hard_attn": hard_attn_2})

        x = x.permute(1, 0, 2)  # x: LND

        return x, mid_states

if __name__ == "__main__":
    model = SegViT(768, patch_size=16, input_resolution=224, first_stage_layer=10)
    input = torch.randn(2, 197, 768)

    model.eval()
    with torch.no_grad():
        input = input.permute(1, 0, 2)  # x: LBD
        o, mid_states = model(input, None, 1)
        o = o.permute(1, 0, 2)  # x: BLD
        print(o.size())

    print("===================")
    for k_, v_ in mid_states.items():
        if v_ is not None:
            if isinstance(v_, list):
                for l_ in v_:
                    print(l_["soft_attn"].size(), l_["hard_attn"].size())
            else:
                print(v_.size())