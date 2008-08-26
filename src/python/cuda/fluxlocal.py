"""Interface with Nvidia CUDA."""

from __future__ import division

__copyright__ = "Copyright (C) 2008 Andreas Kloeckner"

__license__ = """
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see U{http://www.gnu.org/licenses/}.
"""



import numpy
import numpy.linalg as la
from pytools import memoize_method, memoize
import hedge.optemplate
import pycuda.driver as cuda
import pycuda.gpuarray as gpuarray
import pymbolic.mapper.stringifier




class FluxLocalKernel(object):
    def __init__(self, discr):
        self.discr = discr

    @memoize_method
    def get_kernel(self, is_lift, elgroup):
        from hedge.cuda.cgen import \
                Pointer, POD, Value, ArrayOf, Const, \
                Module, FunctionDeclaration, FunctionBody, Block, \
                Comment, Line, \
                CudaShared, CudaConstant, CudaGlobal, Static, \
                Define, \
                Constant, Initializer, If, For, Statement, Assign, \
                ArrayInitializer
                
        discr = self.discr
        d = discr.dimensions
        dims = range(d)
        fplan = discr.flux_plan
        lplan = fplan.flux_lifting_plan()

        liftmat_data = self.gpu_liftmat(is_lift)

        float_type = fplan.float_type

        f_decl = CudaGlobal(FunctionDeclaration(Value("void", "apply_lift_mat"), 
            [
                Pointer(POD(float_type, "flux")),
                Pointer(POD(numpy.uint8, "gmem_lift_mat")),
                Pointer(POD(float_type, "debugbuf")),
                ]
            ))

        rst_channels = discr.devdata.make_valid_tex_channel_count(d)
        cmod = Module([
                Value("texture<float, 1, cudaReadModeElementType>", 
                    "fluxes_on_faces_tex"),
                ])
        if is_lift:
            cmod.append(
                Value("texture<float, 1, cudaReadModeElementType>",
                    "inverse_jacobians_tex"),
                )

        cmod.extend([
                Line(),
                Define("DIMENSIONS", discr.dimensions),
                Define("DOFS_PER_EL", fplan.dofs_per_el()),
                Define("FACES_PER_EL", fplan.faces_per_el()),
                Define("DOFS_PER_FACE", fplan.dofs_per_face()),
                Define("FACE_DOFS_PER_EL", "(DOFS_PER_FACE*FACES_PER_EL)"),
                Line(),
                Define("CHUNK_DOF", "threadIdx.x"),
                Define("PAR_MB_NR", "threadIdx.y"),
                Line(),
                Define("MB_CHUNK", "blockIdx.x"),
                Define("MACROBLOCK_NR", "blockIdx.y"),
                Line(),
                Define("CHUNK_DOF_COUNT", lplan.chunk_size),
                Define("MB_CHUNK_COUNT", lplan.chunks_per_microblock()),
                Define("MB_DOF_COUNT", fplan.microblock.aligned_floats),
                Define("MB_FACEDOF_COUNT", fplan.aligned_face_dofs_per_microblock()),
                Define("MB_EL_COUNT", fplan.microblock.elements),
                Define("PAR_MB_COUNT", lplan.parallelism.p),
                Define("SEQ_MB_COUNT", lplan.parallelism.s),
                Line(),
                Define("THREAD_NUM", "(CHUNK_DOF+PAR_MB_NR*CHUNK_DOF_COUNT)"),
                Define("COALESCING_THREAD_COUNT", "(PAR_MB_COUNT*CHUNK_DOF_COUNT)"),
                Line(),
                Define("MB_DOF_BASE", "(MB_CHUNK*CHUNK_DOF_COUNT)"),
                Define("MB_DOF", "(MB_DOF_BASE+CHUNK_DOF)"),
                Define("GLOBAL_MB_NR_BASE", "(MACROBLOCK_NR*PAR_MB_COUNT*SEQ_MB_COUNT)"),
                Line(),
                Define("LIFTMAT_COLUMNS", liftmat_data.matrix_columns),
                Define("LIFTMAT_CHUNK_FLOATS", liftmat_data.block_floats),
                Define("LIFTMAT_CHUNK_BYTES", 
                    "(LIFTMAT_CHUNK_FLOATS*%d)" % fplan.float_size),

                Line(),
                CudaShared(ArrayOf(POD(float_type, "smem_lift_mat"), 
                    "LIFTMAT_CHUNK_FLOATS")),
                CudaShared(
                    ArrayOf(
                        ArrayOf(
                            POD(float_type, "dof_buffer"), 
                            "PAR_MB_COUNT"),
                        "CHUNK_DOF_COUNT"),
                    ),
                CudaShared(POD(numpy.uint16, "chunk_start_el")),
                CudaShared(POD(numpy.uint16, "chunk_stop_el")),
                CudaShared(POD(numpy.uint16, "chunk_el_count")),
                Line(),
                ArrayInitializer(
                        CudaConstant(
                            ArrayOf(
                                POD(numpy.uint16, "chunk_start_el_lookup"),
                            "MB_CHUNK_COUNT")),
                        [(chk*lplan.chunk_size)//fplan.dofs_per_el()
                            for chk in range(lplan.chunks_per_microblock())]
                        ),
                ArrayInitializer(
                        CudaConstant(
                            ArrayOf(
                                POD(numpy.uint16, "chunk_stop_el_lookup"),
                            "MB_CHUNK_COUNT")),
                        [min(fplan.microblock.elements, 
                            (chk*lplan.chunk_size+lplan.chunk_size-1)
                                //fplan.dofs_per_el()+1)
                            for chk in range(lplan.chunks_per_microblock())]
                        ),
                ])

        S = Statement
        f_body = Block()
            
        f_body.extend_log_block("calculate responsibility data", [
            Initializer(POD(numpy.uint8, "dof_el"),
                "MB_DOF/DOFS_PER_EL"),
            Line(),

            If("THREAD_NUM==0",
                Block([
                    Assign("chunk_start_el", "chunk_start_el_lookup[MB_CHUNK]"),
                    Assign("chunk_stop_el", "chunk_stop_el_lookup[MB_CHUNK]"),
                    Assign("chunk_el_count", "chunk_stop_el-chunk_start_el")
                    ])
                ),
            S("__syncthreads()")
            ])

        from hedge.cuda.tools import get_load_code
        f_body.extend(
            get_load_code(
                dest="smem_lift_mat",
                base=("gmem_lift_mat + MB_CHUNK*LIFTMAT_CHUNK_BYTES"),
                bytes="LIFTMAT_CHUNK_BYTES",
                descr="load lift mat chunk")
            )

        # ---------------------------------------------------------------------
        def get_batched_fetch_mat_mul_code(el_fetch_count):
            result = []
            dofs = range(fplan.face_dofs_per_el())

            for load_chunk_start in range(0, fplan.face_dofs_per_el(),
                    lplan.chunk_size):
                result.append(
                        Assign(
                            "dof_buffer[PAR_MB_NR][CHUNK_DOF]",
                            "tex1Dfetch(fluxes_on_faces_tex, "
                            "global_mb_facedof_base"
                            "+(chunk_start_el)*FACE_DOFS_PER_EL+%d+CHUNK_DOF)"
                            % (load_chunk_start)
                            ))
            
                result.extend([
                        S("__syncthreads()"),
                        Line(),
                        ])

                for dof in dofs[load_chunk_start:load_chunk_start+lplan.chunk_size]:
                    result.append(
                            S("result += "
                                "smem_lift_mat[CHUNK_DOF*LIFTMAT_COLUMNS + %d]"
                                "*"
                                "dof_buffer[PAR_MB_NR][%d]"
                                % (dof, dof-load_chunk_start))
                            )
                result.append(Line())
            return result

        def get_direct_tex_mat_mul_code():
            return [
                    S("result += "
                        "tex1Dfetch(fluxes_on_faces_tex, "
                        "global_mb_facedof_base"
                        "+dof_el*FACE_DOFS_PER_EL+%(j)d)"
                        " * smem_lift_mat["
                        "%(row)s*LIFTMAT_COLUMNS + %(j)s"
                        "]"
                        % {"j":j, "row": "CHUNK_DOF"}
                        )
                    for j in range(
                        fplan.dofs_per_face()*fplan.faces_per_el())
                    ]+[Line()]

        def get_mat_mul_code(el_fetch_count):
            if el_fetch_count == 1:
                return get_batched_fetch_mat_mul_code(el_fetch_count)
            else:
                return get_direct_tex_mat_mul_code()

        if is_lift:
            inv_jac_multiplier = ("tex1Dfetch(inverse_jacobians_tex,"
                    "global_mb_nr*MB_EL_COUNT+dof_el)")
        else:
            inv_jac_multiplier = "1"

        from hedge.cuda.cgen import make_multiple_ifs
        f_body.append(make_multiple_ifs([
                ("chunk_el_count == %d" % fetch_count,
                    For("unsigned short seq_mb_number = 0",
                        "seq_mb_number < SEQ_MB_COUNT",
                        "++seq_mb_number",
                        Block([
                            Initializer(POD(numpy.uint32, "global_mb_nr"),
                                "GLOBAL_MB_NR_BASE + seq_mb_number*PAR_MB_COUNT + PAR_MB_NR"),
                            Initializer(POD(numpy.uint32, "global_mb_dof_base"),
                                "global_mb_nr*MB_DOF_COUNT"),
                            Initializer(POD(numpy.uint32, "global_mb_facedof_base"),
                                "global_mb_nr*MB_FACEDOF_COUNT"),
                            Line(),
                            Initializer(POD(float_type, "result"), 0),
                            Line(),
                            ]
                            +get_mat_mul_code(fetch_count)+[
                            If("MB_DOF < DOFS_PER_EL*MB_EL_COUNT",
                                Assign(
                                    "flux[global_mb_dof_base+MB_DOF]",
                                    "result*%s" % inv_jac_multiplier
                                    )
                                )
                            ])
                        )
                    )
                for fetch_count in 
                range(1, lplan.max_elements_touched_by_chunk()+1)]
                ))

        # finish off ----------------------------------------------------------
        cmod.append(FunctionBody(f_decl, f_body))

        mod = cuda.SourceModule(cmod, 
                keep=True, 
                #options=["--maxrregcount=12"]
                )
        print "lift: lmem=%d smem=%d regs=%d" % (mod.lmem, mod.smem, mod.registers)

        fluxes_on_faces_texref = mod.get_texref("fluxes_on_faces_tex")
        texrefs = [fluxes_on_faces_texref]

        if is_lift:
            inverse_jacobians_texref = mod.get_texref("inverse_jacobians_tex")
            self.inverse_jacobians_tex(elgroup).bind_to_texref(
                    inverse_jacobians_texref)
            texrefs.append(inverse_jacobians_texref)

        return (mod.get_function("apply_lift_mat"), 
                texrefs, 
                fluxes_on_faces_texref)

    @memoize_method
    def gpu_liftmat(self, is_lift):
        discr = self.discr
        fplan = discr.flux_plan
        lplan = fplan.flux_lifting_plan()

        columns = fplan.face_dofs_per_el()
        # avoid smem fetch bank conflicts by ensuring odd col count
        if columns % 2 == 0:
            columns += 1

        block_floats = self.discr.devdata.align_dtype(
                columns*lplan.chunk_size, fplan.float_size)

        if is_lift:
            mat = fplan.ldis.lifting_matrix()
        else:
            mat = fplan.ldis.multi_face_mass_matrix()

        vstacked_matrix = numpy.vstack(
                fplan.microblock.elements*(mat,)
                )

        if vstacked_matrix.shape[1] < columns:
            vstacked_matrix = numpy.hstack((
                vstacked_matrix,
                numpy.zeros((
                    vstacked_matrix.shape[0],
                    columns-vstacked_matrix.shape[1]
                    ))
                ))
                
        chunks = [
                buffer(numpy.asarray(
                    vstacked_matrix[
                        chunk_start:chunk_start+lplan.chunk_size],
                    dtype=self.discr.flux_plan.float_type,
                    order="C"))
                for chunk_start in range(
                    0, fplan.microblock.elements*fplan.dofs_per_el(), 
                    lplan.chunk_size)
                ]
        
        from hedge.cuda.tools import pad_and_join

        from pytools import Record
        class GPULiftMatrices(Record): pass

        return GPULiftMatrices(
                device_memory=cuda.to_device(
                    pad_and_join(chunks, block_floats*fplan.float_size)),
                block_floats=block_floats,
                matrix_columns=columns,
                )

    # data blocks -------------------------------------------------------------
    @memoize_method
    def inverse_jacobians_tex(self, elgroup):
        ij = elgroup.inverse_jacobians[
                    self.discr.elgroup_microblock_indices(elgroup)]
        return gpuarray.to_gpu(
                ij.astype(self.discr.flux_plan.float_type))
