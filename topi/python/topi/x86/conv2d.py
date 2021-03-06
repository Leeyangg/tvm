# pylint: disable=invalid-name,unused-variable,unused-argument,no-member
"""Conv2D schedule on x86"""
import tvm
from tvm import autotvm
from tvm.autotvm.task.nnvm_integration import deserialize_args
from tvm.autotvm.task import get_config
from .. import generic, tag
from .. import nn
from ..util import get_const_tuple
from ..nn.conv2d import conv2d, conv2d_NCHWc, conv2d_alter_layout, _get_workload
from ..nn.pad import pad

from . import conv2d_avx_1x1, conv2d_avx_common

def _get_fp32_len():
    fp32_vec_len = 8
    target = tvm.target.current_target()
    if target is not None:
        for opt in target.options:
            if opt == '-mcpu=skylake-avx512':
                fp32_vec_len = 16
    return fp32_vec_len


def _get_default_config(cfg, workload):
    """
    Get default schedule config for the workload
    Parameters
    ----------
    workload : topi.nn.conv2d.Workload
        Convolution workload
    """
    fp32_vec_len = _get_fp32_len()
    is_kernel_1x1 = workload.hkernel == 1 and workload.wkernel == 1
    if is_kernel_1x1:
        conv2d_avx_1x1._fallback_schedule(cfg, workload, fp32_vec_len)
    else:
        conv2d_avx_common._fallback_schedule(cfg, workload, fp32_vec_len)


def _create_tuning_space(cfg, data, kernel, strides, padding, layout):
    """Create schedule configuration from input arguments"""
    dshape = get_const_tuple(data.shape)
    kshape = get_const_tuple(kernel.shape)
    if layout == 'NCHW':
        n, ic, h, w = dshape
        oc, _, kh, kw = kshape
    else:
        raise ValueError("Not support this layout {} with "
                         "schedule template.".format(layout))
    is_kernel_1x1 = kh == 1 and kw == 1
    ph, pw = padding if isinstance(padding, (tuple, list)) else (padding, padding)
    sh, sw = strides if isinstance(strides, (tuple, list)) else (strides, strides)
    oh = (h - kh + 2 * ph) // sh + 1
    ow = (w - kw + 2 * pw) // sw + 1

    # Create schedule config
    cfg.define_split("tile_ic", ic, num_outputs=2)
    cfg.define_split("tile_oc", oc, num_outputs=2)
    cfg.define_split("tile_ow", ow, num_outputs=2, filter=lambda y: y.size[-1] <= 64)
    if is_kernel_1x1:
        cfg.define_knob("tile_oh", [1, 2] if oh > 1 else [1])
    else:
        cfg.define_knob("unroll_kw", [True, False])


@autotvm.register_topi_compute(conv2d, 'cpu', 'direct')
def _declaration_conv(cfg, data, kernel, strides, padding, layout, out_dtype):
    out_dtype = data.dtype if out_dtype is None else out_dtype
    padding = padding if isinstance(padding, (tuple, list)) else (padding, padding)
    strides = strides if isinstance(strides, (tuple, list)) else (strides, strides)
    if layout == 'NCHW':
        _create_tuning_space(cfg, data, kernel, strides, padding, layout)
        if cfg.is_fallback:
            wkl = _get_workload(data, kernel, strides, padding, out_dtype)
            _get_default_config(cfg, wkl)
        return _declaration_conv_impl(cfg, data, kernel, strides, padding, layout, out_dtype)
    elif layout == 'HWCN':
        return nn.conv2d_hwcn(data, kernel, strides, padding, out_dtype)
    elif layout == 'NHWC':
        return nn.conv2d_nhwc(data, kernel, strides, padding, out_dtype)
    else:
        raise ValueError("not support this layout {} yet".format(layout))


def _declaration_conv_impl(cfg, data, kernel, strides, padding, layout, out_dtype):
    out_dtype = data.dtype if out_dtype is None else out_dtype
    assert layout == 'NCHW', "only support NCHW convolution for AVX"

    HPAD, WPAD = padding
    HSTR, WSTR = strides

    batch_size, in_channel, in_height, in_width = get_const_tuple(data.shape)
    num_filter, _, kernel_height, kernel_width = get_const_tuple(kernel.shape)

    pad_height = in_height + 2 * HPAD
    pad_width = in_width + 2 * WPAD

    out_height = (in_height + 2 * HPAD - kernel_height) // HSTR + 1
    out_width = (in_width + 2 * WPAD - kernel_width) // WSTR + 1

    # pack data
    DOPAD = (HPAD != 0 or WPAD != 0)
    if DOPAD:
        data_pad = pad(data, (0, 0, HPAD, WPAD), name="data_pad")
    else:
        data_pad = data

    # fetch schedule
    ic_bn, oc_bn = cfg["tile_ic"].size[-1], cfg["tile_oc"].size[-1]

    shape = (batch_size, in_channel // ic_bn, pad_height, ic_bn, pad_width)
    data_vec = tvm.compute(shape,
                           lambda n, C, h, c, w: data_pad[n, C * ic_bn + c, h, w],
                           name='data_vec')

    # pack kernel
    shape = (num_filter//oc_bn, in_channel//ic_bn,
             kernel_height, kernel_width, ic_bn, oc_bn)
    kernel_vec = tvm.compute(shape,
                             lambda CO, CI, h, w, ci, co:
                             kernel[CO * oc_bn + co, CI * ic_bn + ci, h, w],
                             name='kernel_vec')

    # convolution
    oshape = (batch_size, num_filter//oc_bn, out_height, out_width, oc_bn)
    unpack_shape = (batch_size, num_filter, out_height, out_width)

    ic = tvm.reduce_axis((0, in_channel), name='ic')
    kh = tvm.reduce_axis((0, kernel_height), name='kh')
    kw = tvm.reduce_axis((0, kernel_width), name='kw')

    conv = tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
                       tvm.sum(data_vec[n, ic//ic_bn, oh*HSTR+kh, ic%ic_bn,
                                        ow*WSTR+kw].astype(out_dtype) *
                               kernel_vec[oc_chunk, ic//ic_bn, kh, kw, ic%ic_bn,
                                          oc_block].astype(out_dtype),
                               axis=[ic, kh, kw]), name='conv')

    unpack = tvm.compute(unpack_shape,
                         lambda n, c, h, w: conv[n, c // oc_bn, h, w, c % oc_bn]
                         .astype(out_dtype),
                         name='output_unpack',
                         tag='conv2d_nchw')
    return unpack


@autotvm.register_topi_schedule(generic.schedule_conv2d_nchw, 'cpu', ['direct'])
def schedule_conv2d(cfg, outs):
    """Create schedule for tensors"""
    s = tvm.create_schedule([x.op for x in outs])
    scheduled_ops = []

    def traverse(op):
        """Traverse operators from computation graph"""
        # inline all one-to-one-mapping operators except the last stage (output)
        if tag.is_broadcast(op.tag):
            if op not in s.outputs:
                s[op].compute_inline()
            for tensor in op.input_tensors:
                if tensor.op.input_tensors and tensor.op not in scheduled_ops:
                    traverse(tensor.op)

        if 'conv2d_nchw' in op.tag:
            output = op.output(0)
            conv_out = op.input_tensors[0]
            kernel_vec = conv_out.op.input_tensors[1]
            kernel = kernel_vec.op.input_tensors[0]
            if isinstance(kernel.op, tvm.tensor.ComputeOp) and "dilate" in kernel.op.tag:
                s[kernel].compute_inline()
            data_vec = conv_out.op.input_tensors[0]
            data = data_vec.op.input_tensors[0]
            data_pad = None
            if isinstance(data.op, tvm.tensor.ComputeOp) and "pad" in data.op.tag:
                data_pad = data
                data = data_pad.op.input_tensors[0]

            _, _, kh, kw = get_const_tuple(kernel.shape)
            is_kernel_1x1 = kh == 1 and kw == 1
            args = [s, cfg, data, data_pad, data_vec, kernel_vec, conv_out, output, outs[0]]
            if is_kernel_1x1:
                conv2d_avx_1x1._schedule_conv(*args)
            else:
                conv2d_avx_common._schedule_conv(*args)

        scheduled_ops.append(op)

    traverse(outs[0].op)
    return s


@generic.schedule_conv2d_nhwc.register("cpu")
def schedule_conv2d_nhwc(outs):
    """Create schedule for tensors"""
    s = tvm.create_schedule([x.op for x in outs])
    output_op = outs[0].op
    scheduled_ops = []

    def traverse(op):
        """Traverse operators from computation graph"""
        # inline all one-to-one-mapping operators except the last stage (output)
        if tag.is_broadcast(op.tag):
            if op not in s.outputs:
                s[op].compute_inline()
            else: # inject custom schedule
                if len(op.axis) == 4: # schedule bias + bn + relu
                    n, h, w, c = op.axis
                    fused = s[op].fuse(n, h, w)
                    s[op].parallel(fused)
                    s[op].vectorize(c)
            for tensor in op.input_tensors:
                if tensor.op.input_tensors and tensor.op not in scheduled_ops:
                    traverse(tensor.op)

        if 'conv2d_nhwc' in op.tag:
            conv = op.output(0)
            kernel = op.input_tensors[1]
            if isinstance(kernel.op, tvm.tensor.ComputeOp) and "dilate" in kernel.op.tag:
                s[kernel].compute_inline()

            data = op.input_tensors[0]
            data_pad = None
            if isinstance(data.op, tvm.tensor.ComputeOp) and "pad" in data.op.tag:
                data_pad = data
                data = data_pad.op.input_tensors[0]

            n_pad, h_pad, w_pad, c_pad = data_pad.op.axis
            pad_fused = s[data_pad].fuse(n_pad, h_pad)
            s[data_pad].parallel(pad_fused)
            C = conv
            n, h, w, c = C.op.axis
            ry, rx, rc = C.op.reduce_axis
            n_out, h_out, w_out, c_out = output_op.axis
            s[C].vectorize(c)
            if op != output_op: # fuse bias + bn + relu into conv
                s[C].compute_at(s[output_op], c_out)
            else:
                fused = s[C].fuse(n, h, w)
                s[C].parallel(fused)

        scheduled_ops.append(op)

    traverse(output_op)
    return s


# Define template function for autotvm task
# We define schedule template in this function instead of
# declaration function since actual input arguments need
# to be altered by the schedule selected.
@autotvm.task.register("topi_x86_conv2d_NCHWc")
def _topi_nn_conv2d_NCHWc(*args, **kwargs):
    assert not kwargs, "Do not support kwargs in template function call"
    data, kernel, strides, padding, origin_layout, dtype = deserialize_args(args)
    raw_data_shape = get_const_tuple(data.shape)
    raw_kernel_shape = get_const_tuple(kernel.shape)

    # get config here
    cfg = get_config()
    _create_tuning_space(cfg, data, kernel, strides, padding, origin_layout)

    # change shape with the value in config
    ic_bn, oc_bn, ow_bn = (cfg["tile_ic"].size[-1], cfg["tile_oc"].size[-1],
                           cfg["tile_ow"].size[-1])
    new_data_shape = (raw_data_shape[0], raw_data_shape[1] // ic_bn,
                      raw_data_shape[2], raw_data_shape[3], ic_bn)
    data_layout = "NCHW%dc" % ic_bn
    out_layout = "NCHW%dc" % oc_bn
    new_kernel_shape = (raw_kernel_shape[0] // oc_bn, raw_kernel_shape[1] // ic_bn,
                        raw_kernel_shape[2], raw_kernel_shape[3], ic_bn, oc_bn)
    new_data = tvm.placeholder(new_data_shape, data.dtype)
    new_kernel = tvm.placeholder(new_kernel_shape, kernel.dtype)

    C = _declaration_conv_NCHWc(cfg, new_data, new_kernel, strides, padding,
                                data_layout, out_layout, dtype)
    s = _schedule_conv2d_NCHWc(cfg, [C])
    return s, [new_data, new_kernel, C]


@conv2d_alter_layout.register("cpu")
def _alter_conv2d_layout(attrs, inputs, tinfo):
    import nnvm.symbol as sym
    copy_inputs = [s for s in inputs]
    new_attrs = {k : attrs[k] for k in attrs.keys()}
    data, kernel = tinfo[0], tinfo[1]
    # only optimize for NCHW, groups=1 conv
    if attrs['layout'] != 'NCHW' or attrs.get_int("groups") != 1:
        return None
    batch_size, in_channel, height, width = get_const_tuple(data.shape)
    out_channel, _, kh, kw = get_const_tuple(kernel.shape)

    padding = attrs.get_int_tuple("padding")
    strides = attrs.get_int_tuple("strides")
    layout = attrs['layout']

    dtype = data.dtype
    out_dtype = dtype if attrs["out_dtype"] == "same" else attrs["out_dtype"]

    workload = autotvm.task.args_to_workload(
        [data, kernel, strides, padding, layout, out_dtype], conv2d)
    dispatch_ctx = autotvm.task.DispatchContext.current
    target = tvm.target.current_target()
    cfg = dispatch_ctx.query(target, workload)
    if cfg.is_fallback:
        wkl = _get_workload(data, kernel, strides, padding, out_dtype)
        _get_default_config(cfg, wkl)

    ic_bn, oc_bn = cfg["tile_ic"].size[-1], cfg["tile_oc"].size[-1]
    new_attrs['layout'] = 'NCHW%dc' % ic_bn
    new_attrs['out_layout'] = 'NCHW%dc' % oc_bn
    # (oc, ic, h, w) -> (OC, IC, h, w, ic, oc)
    new_attrs['kernel_layout'] = 'OIHW%di%do' % (ic_bn, oc_bn)

    # Store the same config for the altered operator (workload)
    new_data = tvm.placeholder((batch_size, in_channel//ic_bn, height, width, ic_bn),
                               dtype=data.dtype)
    new_kernel = tvm.placeholder((out_channel//oc_bn, in_channel//ic_bn, kh, kw, ic_bn, oc_bn),
                                 dtype=kernel.dtype)
    new_workload = autotvm.task.args_to_workload(
        [new_data, new_kernel, strides, padding, new_attrs['layout'],
         new_attrs['out_layout'], out_dtype], conv2d_NCHWc)
    dispatch_ctx.update(target, new_workload, cfg)

    return sym.contrib.conv2d_NCHWc(*copy_inputs, **new_attrs)


@autotvm.register_topi_compute(conv2d_NCHWc, 'cpu', 'direct')
def _declaration_conv_NCHWc(cfg, data, kernel, strides,
                            padding, layout, out_layout, out_dtype):
    # layout and out_layout are not used here,
    # we keep them for debug convenience when dumping autotvm workload
    HPAD, WPAD = padding if isinstance(padding, (tuple, list)) else (padding, padding)
    HSTR, WSTR = strides if isinstance(strides, (tuple, list)) else (strides, strides)

    n, ic_chunk, ih, iw, ic_bn = get_const_tuple(data.shape)
    in_channel = ic_chunk * ic_bn
    if data.dtype == 'uint8':
        oc_chunk, _, kernel_height, kernel_width, _, oc_bn, _ = get_const_tuple(kernel.shape)
    else:
        oc_chunk, _, kernel_height, kernel_width, _, oc_bn = get_const_tuple(kernel.shape)
    num_filter = oc_chunk * oc_bn

    # get workload and related schedule config
    wkl = _get_workload(tvm.placeholder((n, in_channel, ih, iw), dtype=data.dtype),
                        tvm.placeholder((num_filter, in_channel, kernel_height, kernel_width),
                                        dtype=kernel.dtype),
                        strides, padding, out_dtype)
    if cfg.is_fallback:
        _get_default_config(cfg, wkl)

    # output shape
    out_height = (ih + 2 * HPAD - kernel_height) // HSTR + 1
    out_width = (iw + 2 * WPAD - kernel_width) // WSTR + 1
    oshape = (n, oc_chunk, out_height, out_width, oc_bn)

    # DOPAD
    DOPAD = (HPAD != 0 or WPAD != 0)
    if DOPAD:
        data_pad = pad(data, (0, 0, HPAD, WPAD, 0), name="data_pad")
    else:
        data_pad = data

    ic = tvm.reduce_axis((0, in_channel), name='ic')
    kh = tvm.reduce_axis((0, kernel_height), name='kh')
    kw = tvm.reduce_axis((0, kernel_width), name='kw')

    if data.dtype == 'uint8':
        assert out_dtype == "int32", \
            "INT8 convolution requires input dtype = uint8 and output dtype=int32"
        # Intel performs dot product of 2 "4" Int8 values
        # Current implementation requires ic_bn to be a multiple of 4
        n_elems = 4
        assert ic_bn % n_elems == 0

        ic_outer = tvm.reduce_axis((0, wkl.in_filter//ic_bn), name='ic_outer')
        ic_f_inner = tvm.reduce_axis((0, ic_bn//n_elems), name='ic_f_inner')
        ic_s_inner = tvm.reduce_axis((0, n_elems), name='ic_s_inner')
        return tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
                           tvm.sum(data_pad[n, ic_outer, oh*HSTR+kh, ow*WSTR+kw,
                                            ic_f_inner * n_elems +  ic_s_inner]
                                   .astype(out_dtype) *
                                   kernel[oc_chunk, ic_outer, kh, kw, ic_f_inner,
                                          oc_block, ic_s_inner].astype(out_dtype),
                                   axis=[kh, kw, ic_outer, ic_f_inner, ic_s_inner]),
                           name='conv2d_NCHWc_int8', tag="conv2d_NCHWc_int8")
    # else: fp implementation
    return tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
                       tvm.sum(data_pad[n, ic//ic_bn, oh*HSTR+kh, ow*WSTR+kw,
                                        ic%ic_bn].astype(out_dtype) *
                               kernel[oc_chunk, ic//ic_bn, kh, kw, ic%ic_bn, oc_block],
                               axis=[ic, kh, kw]),
                       name='conv2d_NCHWc', tag="conv2d_NCHWc")


@autotvm.register_topi_schedule(generic.schedule_conv2d_NCHWc, 'cpu', ['direct'])
def _schedule_conv2d_NCHWc(cfg, outs):
    """Create schedule for tensors"""
    s = tvm.create_schedule([x.op for x in outs])
    scheduled_ops = []

    def traverse(op):
        """Traverse operators from computation graph"""
        # inline all one-to-one-mapping operators except the last stage (output)
        if tag.is_broadcast(op.tag):
            if op not in s.outputs:
                s[op].compute_inline()
            for tensor in op.input_tensors:
                if tensor.op.input_tensors and tensor.op not in scheduled_ops:
                    traverse(tensor.op)

        if 'conv2d_NCHWc' in op.tag:
            conv_out = op.output(0)
            kernel = conv_out.op.input_tensors[1]
            data_vec = conv_out.op.input_tensors[0]
            data = data_vec.op.input_tensors[0] \
                if isinstance(data_vec.op, tvm.tensor.ComputeOp) and "pad" not in data_vec.op.tag \
                else data_vec
            if isinstance(data.op, tvm.tensor.ComputeOp) and "pad" in data.op.tag:
                data_pad = data
                data = data_pad.op.input_tensors[0]

            args = [s, cfg, data_vec, conv_out, outs[0]]
            if data.dtype == 'uint8':
                # int8 conv kernel is 7-dim
                _, _, kh, kw, _, _, _ = get_const_tuple(kernel.shape)
                if kh == 1 and kw == 1:
                    conv2d_avx_1x1._schedule_conv_NCHWc_int8(*args)
                else:
                    conv2d_avx_common._schedule_conv_NCHWc_int8(*args)
            else:
                _, _, kh, kw, _, _, = get_const_tuple(kernel.shape)
                if kh == 1 and kw == 1:
                    conv2d_avx_1x1._schedule_conv_NCHWc(*args)
                else:
                    conv2d_avx_common._schedule_conv_NCHWc(*args)

        scheduled_ops.append(op)

    traverse(outs[0].op)
    return s
