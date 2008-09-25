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
from pytools import memoize_method, memoize
import pycuda.driver as cuda
import pycuda.gpuarray as gpuarray
from hedge.cuda.tools import FakeGPUArray
import hedge.cuda.plan 




# plan ------------------------------------------------------------------------
class SMemFieldFluxLocalExecutionPlan(hedge.cuda.plan.SMemFieldLocalOpExecutionPlan):
    def registers(self):
        return 16

    @memoize_method
    def shared_mem_use(self):
        given = self.given
        
        return (64 # parameters, block header, small extra stuff
               + given.float_size() * (
                   self.parallelism.p * self.given.aligned_face_dofs_per_microblock()))

    def make_kernel(self, discr):
        return SMemFieldFluxLocalKernel(discr, self)




# kernel ----------------------------------------------------------------------
class SMemFieldFluxLocalKernel(object):
    def __init__(self, discr, plan):
        self.discr = discr
        self.plan = plan

        fplan = discr.flux_plan

        from hedge.cuda.tools import int_ceiling
        self.grid = (int_ceiling(
                len(discr.blocks)*fplan.dofs_per_block()
                / self.plan.dofs_per_macroblock()),
                1)

    def benchmark(self):
        discr = self.discr
        given = discr.given
        elgroup, = discr.element_groups

        is_lift = True
        lift = self.get_kernel(is_lift, elgroup)

        def vol_empty():
            from hedge.cuda.tools import int_ceiling
            dofs = int_ceiling(
                    discr.flux_plan.dofs_per_block() * len(discr.blocks),     
                    self.plan.dofs_per_macroblock())

            return gpuarray.empty((dofs,), dtype=given.float_type,
                    allocator=discr.pool.allocate)

        flux = vol_empty()
        fluxes_on_faces = gpuarray.empty(
                discr.flux_plan.fluxes_on_faces_shape(len(discr.blocks)), 
                dtype=given.float_type,
                allocator=discr.pool.allocate)

        if set(["cuda_lift", "cuda_debugbuf"]) <= discr.debug:
            debugbuf = gpuarray.zeros((1024,), dtype=numpy.float32)
        else:
            debugbuf = FakeGPUArray()

        count = 20

        start = cuda.Event()
        start.record()
        cuda.Context.synchronize()
        for i in range(count):
            lift.prepared_call(self.grid,
                    flux.gpudata, 
                    fluxes_on_faces.gpudata,
                    0)
        stop = cuda.Event()
        stop.record()
        stop.synchronize()

        return 1e-3/count * stop.time_since(start)

    def __call__(self, fluxes_on_faces, is_lift):
        discr = self.discr
        elgroup, = discr.element_groups

        lift = self.get_kernel(is_lift, elgroup)

        flux = discr.volume_empty() 

        if set(["cuda_lift", "cuda_debugbuf"]) <= discr.debug:
            debugbuf = gpuarray.zeros((1024,), dtype=numpy.float32)
        else:
            debugbuf = FakeGPUArray()

        if discr.instrumented:
            kernel_time = lift.prepared_timed_call(self.grid, 
                    flux.gpudata, 
                    fluxes_on_faces.gpudata,
                    debugbuf.gpudata)
            
            discr.inner_flux_timer.add_time(kernel_time)
            discr.inner_flux_counter.add()
        else:
            lift.prepared_call(self.grid,
                    flux.gpudata, 
                    fluxes_on_faces.gpudata,
                    debugbuf.gpudata)

        if set(["cuda_lift", "cuda_debugbuf"]) <= discr.debug:
            copied_debugbuf = debugbuf.get()[:144*7].reshape((144,7))
            print "DEBUG"
            numpy.set_printoptions(linewidth=100)
            copied_debugbuf.shape = (144,7)
            numpy.set_printoptions(threshold=3000)

            print copied_debugbuf
            raw_input()

        return flux

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
        given = discr.given

        float_type = given.float_type

        f_decl = CudaGlobal(FunctionDeclaration(Value("void", "apply_lift_mat_smem"), 
            [
                Pointer(POD(float_type, "flux")),
                Pointer(POD(float_type, "fluxes_on_faces")),
                Pointer(POD(float_type, "debugbuf")),
                ]
            ))

        cmod = Module([
                Value("texture<float, 2, cudaReadModeElementType>", 
                    "lift_mat_tex"),
                ])
        if is_lift:
            cmod.append(
                Value("texture<float, 1, cudaReadModeElementType>",
                    "inverse_jacobians_tex"),
                )

        cmod.extend([
                Line(),
                Define("DIMENSIONS", discr.dimensions),
                Define("DOFS_PER_EL", given.dofs_per_el()),
                Define("FACES_PER_EL", given.faces_per_el()),
                Define("DOFS_PER_FACE", given.dofs_per_face()),
                Define("FACE_DOFS_PER_EL", "(DOFS_PER_FACE*FACES_PER_EL)"),
                Define("MB_EL_COUNT", given.microblock.elements),
                Line(),
                Define("DOFS_PER_MB", "(DOFS_PER_EL*MB_EL_COUNT)"),
                Define("ALIGNED_DOFS_PER_MB", given.microblock.aligned_floats),
                Define("ALIGNED_FACE_DOFS_PER_MB", given.aligned_face_dofs_per_microblock()),
                Line(),
                Define("MB_DOF", "threadIdx.x"),
                Define("PAR_MB_NR", "threadIdx.y"),
                Define("EL_DOF", "(MB_DOF - mb_el*DOFS_PER_EL)"),
                Line(),
                Define("MACROBLOCK_NR", "blockIdx.x"),
                Line(),
                Define("PAR_MB_COUNT", self.plan.parallelism.p),
                Define("SEQ_MB_COUNT", self.plan.parallelism.s),
                Line(),
                Define("THREAD_NUM", "(MB_DOF+PAR_MB_NR*ALIGNED_DOFS_PER_MB)"),
                Line(),
                Define("GLOBAL_MB_NR", 
                    "(MACROBLOCK_NR*PAR_MB_COUNT*SEQ_MB_COUNT "
                    "+ seq_mb_number*PAR_MB_COUNT "
                    "+ PAR_MB_NR)"),
                Line(),
                CudaShared(
                    ArrayOf(
                        ArrayOf(
                            POD(float_type, "smem_fluxes_on_faces"), 
                            "PAR_MB_COUNT"),
                        "ALIGNED_FACE_DOFS_PER_MB")),
                Line(),
                ])

        S = Statement
        f_body = Block([
            Initializer(Const(POD(numpy.uint16, "mb_el")),
                "MB_DOF / DOFS_PER_EL"),
            Line(),
            ])

        def get_lift_code():
            from pytools import flatten

            if is_lift:
                inv_jac_multiplier = ("tex1Dfetch(inverse_jacobians_tex,"
                        "GLOBAL_MB_NR*MB_EL_COUNT+mb_el)")
            else:
                inv_jac_multiplier = "1"

            return Block([
                Comment("everybody needs to be done with the old data"),
                S("__syncthreads()"),
                Line(),
                For("unsigned i = MB_DOF",
                    "i < ALIGNED_FACE_DOFS_PER_MB",
                    "i += ALIGNED_DOFS_PER_MB",
                    Assign("smem_fluxes_on_faces[PAR_MB_NR][i]",
                        "fluxes_on_faces[GLOBAL_MB_NR*ALIGNED_FACE_DOFS_PER_MB + i]"),
                    ),
                Line(),
                Comment("all the new data must be loaded"),
                S("__syncthreads()"),
                Line(),
                Initializer(POD(float_type, "result"), 0),
                Line(),
                If("MB_DOF < DOFS_PER_MB", Block([
                    S("result += tex2D(lift_mat_tex, EL_DOF, %d) "
                    "* smem_fluxes_on_faces[PAR_MB_NR][mb_el*FACE_DOFS_PER_EL+%d]" 
                    % (j, j))
                    for j in range(
                        given.dofs_per_face()*given.faces_per_el())
                    ]+[
                    Line(), 
                    Assign(
                        "flux[GLOBAL_MB_NR*ALIGNED_DOFS_PER_MB + MB_DOF]",
                        "result*%s" % inv_jac_multiplier),
                    ])
                    )
                ])

        def get_mat_mul_code(el_fetch_count):
            if el_fetch_count == 1:
                return get_batched_fetch_mat_mul_code(el_fetch_count)
            else:
                return get_direct_tex_mat_mul_code()

        f_body.append(For("unsigned short seq_mb_number = 0",
            "seq_mb_number < SEQ_MB_COUNT",
            "++seq_mb_number", get_lift_code()))

        # finish off ----------------------------------------------------------
        cmod.append(FunctionBody(f_decl, f_body))

        mod = cuda.SourceModule(cmod, 
                keep=True, 
                #options=["--maxrregcount=12"]
                )
        print "lift: lmem=%d smem=%d regs=%d" % (mod.lmem, mod.smem, mod.registers)

        lift_mat_texref = mod.get_texref("lift_mat_tex")
        lift_mat_texref.set_array(self.gpu_lift_mat(is_lift))
        texrefs = [lift_mat_texref]

        if is_lift:
            inverse_jacobians_texref = mod.get_texref("inverse_jacobians_tex")
            self.inverse_jacobians_tex(elgroup).bind_to_texref(
                    inverse_jacobians_texref)
            texrefs.append(inverse_jacobians_texref)

        func = mod.get_function("apply_lift_mat_smem")
        func.prepare(
                "PPP", 
                block=(given.microblock.aligned_floats, self.plan.parallelism.p, 1),
                texrefs=texrefs)

        return func

    # data blocks -------------------------------------------------------------
    @memoize_method
    def gpu_lift_mat(self, is_lift):
        discr = self.discr
        given = discr.given

        if is_lift:
            mat = given.ldis.lifting_matrix()
        else:
            mat = given.ldis.multi_face_mass_matrix()

        return cuda.matrix_to_array(mat.astype(given.float_type))

    @memoize_method
    def inverse_jacobians_tex(self, elgroup):
        ij = elgroup.inverse_jacobians[
                    self.discr.elgroup_microblock_indices(elgroup)]
        return gpuarray.to_gpu(
                ij.astype(self.discr.given.float_type))


