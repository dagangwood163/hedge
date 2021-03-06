"""Local function space representation."""

from __future__ import division

__copyright__ = "Copyright (C) 2007 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy
import numpy.linalg as la
from hedge.tools import AffineMap
import hedge._internal
from math import sqrt
from pytools import memoize_method
import hedge.mesh.element


# {{{ warping helpers ---------------------------------------------------------

class WarpFactorCalculator:
    """Calculator for Warburton's warp factor.

    See T. Warburton,
    "An explicit construction of interpolation nodes on the simplex"
    Journal of Engineering Mathematics Vol 56, No 3, p. 247-262, 2006
    """

    def __init__(self, N):
        from hedge.quadrature import legendre_gauss_lobatto_points
        from hedge.interpolation import newton_interpolation_function

        # Find lgl and equidistant interpolation points
        r_lgl = legendre_gauss_lobatto_points(N)
        r_eq = numpy.linspace(-1, 1, N + 1)

        self.int_f = newton_interpolation_function(r_eq, r_lgl - r_eq)

    def __call__(self, x):
        if abs(x) > 1-1e-10:
            return 0
        else:
            return self.int_f(x) / (1 - x ** 2)


class FaceVertexMismatch(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class TriangleWarper:
    def __init__(self, alpha, order):
        self.alpha = alpha
        self.warp = WarpFactorCalculator(order)

        cls = TriangleDiscretization

        from pytools import wandering_element
        from hedge.tools import normalize

        vertices = [cls.barycentric_to_equilateral(bary)
                for bary in wandering_element(cls.dimensions + 1)]
        all_vertex_indices = range(cls.dimensions + 1)
        face_vertex_indices = cls.geometry \
                .face_vertices(all_vertex_indices)
        faces_vertices = cls.geometry.face_vertices(vertices)

        edgedirs = [normalize(v2 - v1) for v1, v2 in faces_vertices]
        opp_vertex_indices = [
            (set(all_vertex_indices) - set(fvi)).__iter__().next()
            for fvi in face_vertex_indices]

        self.loop_info = zip(
                face_vertex_indices,
                edgedirs,
                opp_vertex_indices)

    def __call__(self, bp):
        shifts = []

        from operator import add, mul

        for fvi, edgedir, opp_vertex_index in self.loop_info:
            blend = 4 * reduce(mul, (bp[i] for i in fvi))
            warp_amount = blend * self.warp(bp[fvi[1]]-bp[fvi[0]]) \
                    * (1 + (self.alpha*bp[opp_vertex_index])**2)
            shifts.append(warp_amount * edgedir)

        return reduce(add, shifts)

# }}}


TriangleBasisFunction = hedge._internal.TriangleBasisFunction
GradTriangleBasisFunction = hedge._internal.GradTriangleBasisFunction
TetrahedronBasisFunction = hedge._internal.TetrahedronBasisFunction
GradTetrahedronBasisFunction = hedge._internal.GradTetrahedronBasisFunction


# {{{ base classes ------------------------------------------------------------
# {{{ generic base classes ----------------------------------------------------
class LocalDiscretization(object):
    # {{{ numbering -----------------------------------------------------------
    @memoize_method
    def face_count(self):
        return len(self.face_indices())

    @memoize_method
    def face_node_count(self):
        return len(self.face_indices()[0])

    # }}}

    # {{{ matrices ------------------------------------------------------------
    @memoize_method
    def vandermonde(self):
        from hedge.polynomial import generic_vandermonde

        return generic_vandermonde(
                list(self.unit_nodes()),
                list(self.basis_functions()))

    @memoize_method
    def grad_vandermonde(self):
        """Compute the Vandermonde matrices of the grad_basis_functions().
        Return a list of these matrices."""

        from hedge.polynomial import generic_multi_vandermonde

        return generic_multi_vandermonde(
                list(self.unit_nodes()),
                list(self.grad_basis_functions()))

    def _assemble_multi_face_mass_matrix(self, face_mass_matrix):
        """Helper for the function below."""

        fmm_height, fmm_width = face_mass_matrix.shape
        assert fmm_height == self.face_node_count()

        result = numpy.zeros(
                (self.node_count(), self.face_count() * fmm_width),
                dtype=numpy.float)

        for i_face, f_indices in enumerate(self.face_indices()):
            f_indices = numpy.array(f_indices, dtype=numpy.uintp)
            result[f_indices, i_face*fmm_width: (i_face + 1)*fmm_width] = \
                    face_mass_matrix

        return result

    @memoize_method
    def multi_face_mass_matrix(self):
        """Return a matrix that combines the effect of multiple face
        mass matrices applied to a vector of the shape::

            [face_1_dofs]+[face_2_dofs]+...+[face_n_dofs]

        Observe that this automatically maps this vector to a volume
        contribution.
        """
        return self._assemble_multi_face_mass_matrix(self.face_mass_matrix())

    @memoize_method
    def lifting_matrix(self):
        """Return a matrix that combines the effect of the inverse
        mass matrix applied after the multi-face mass matrix to a vector
        of the shape::

            [face_1_dofs]+[face_2_dofs]+...+[face_n_dofs]

        Observe that this automatically maps this vector to a volume
        contribution.
        """
        return numpy.dot(self.inverse_mass_matrix(),
                self.multi_face_mass_matrix())

    def find_diff_mat_permutation(self, target_idx):
        """Find a permuation *p* such that::

            diff_mats = self.differentiation_matrices()
            diff_mats[0][p][:,p] == diff_mats[target_idx]

        The permutation is returned as a numpy array of type intp.
        """

        node_tups = self.node_tuples()

        from pytools import get_read_from_map_from_permutation

        def transpose(tup, i, j):
            l = list(tup)
            l[i], l[j] = l[j], l[i]
            return tuple(l)

        p = numpy.array(get_read_from_map_from_permutation(
                node_tups,
                [transpose(nt, 0, target_idx) for nt in node_tups]),
                dtype=numpy.intp)

        dmats = self.differentiation_matrices()
        assert la.norm(dmats[0][p][:, p] - dmats[target_idx]) < 1e-12

        return p
    # }}}


class OrthonormalLocalDiscretization(LocalDiscretization):
    @memoize_method
    def inverse_mass_matrix(self):
        """Return the inverse of the mass matrix of the unit element
        with respect to the nodal coefficients. Divide by the Jacobian
        to obtain the global mass matrix.
        """

        # see doc/hedge-notes.tm
        v = self.vandermonde()
        return numpy.dot(v, v.T)

    @memoize_method
    def mass_matrix(self):
        """Return the mass matrix of the unit element with respect
        to the nodal coefficients. Multiply by the Jacobian to obtain
        the global mass matrix.
        """

        return numpy.asarray(la.inv(self.inverse_mass_matrix()), order="C")

    @memoize_method
    def differentiation_matrices(self):
        """Return matrices that map the nodal values of a function
        to the nodal values of its derivative in each of the unit
        coordinate directions.
        """

        from hedge.tools import leftsolve
        # see doc/hedge-notes.tm
        v = self.vandermonde()
        return [numpy.asarray(
            leftsolve(v, vdiff), order="C")
            for vdiff in self.grad_vandermonde()]

# }}}


# {{{ simplex base class ------------------------------------------------------

class PkSimplexDiscretization(OrthonormalLocalDiscretization):
    # queries -----------------------------------------------------------------
    @property
    def has_facial_nodes(self):
        return self.order > 0

    # {{{ numbering -----------------------------------------------------------
    def vertex_count(self):
        return self.dimensions + 1

    @memoize_method
    def node_count(self):
        """Return the number of interpolation nodes in this element."""
        d = self.dimensions
        o = self.order
        from operator import mul
        from pytools import factorial
        return int(reduce(mul, (o + 1 + i for i in range(d))) / factorial(d))

    @memoize_method
    def vertex_indices(self):
        """Return the list of the vertices' node indices."""
        from pytools import wandering_element

        node_tup_to_idx = dict(
                (ituple, idx)
                for idx, ituple in enumerate(self.node_tuples()))

        vertex_tuples = [self.dimensions * (0,)] \
                + list(wandering_element(self.dimensions, wanderer=self.order))

        return [node_tup_to_idx[vt] for vt in vertex_tuples]

    @memoize_method
    def face_indices(self):
        """Return a list of face index lists. Each face index list contains
        the local node numbers of the nodes on that face.
        """

        node_tup_to_idx = dict(
                (ituple, idx)
                for idx, ituple in enumerate(self.node_tuples()))

        from pytools import \
                generate_nonnegative_integer_tuples_summing_to_at_most

        enum_order_nodes_gen = \
                generate_nonnegative_integer_tuples_summing_to_at_most(
                        self.order, self.dimensions)

        faces = [[] for i in range(self.dimensions + 1)]

        for node_tup in enum_order_nodes_gen:
            for face_idx in self.faces_for_node_tuple(node_tup):
                faces[face_idx].append(node_tup_to_idx[node_tup])

        return [tuple(fi) for fi in faces]
    # }}}

    # {{{ face operations -----------------------------------------------------
    @memoize_method
    def unit_face_nodes(self):
        """Return the face node locations in facial unit coordinates, e.g.
        per-face *(r,s)* coordinates.

        .. note::

            These nodes are identical for each face.
        """
        unodes = self.unit_nodes()
        face_indices = self.face_indices()

        dim = self.dimensions
        return [unodes[i][:dim-1] for i in face_indices[0]]

    @memoize_method
    def face_vandermonde(self):
        from hedge.polynomial import generic_vandermonde

        return generic_vandermonde(
                self.unit_face_nodes(),
                self.face_basis())

    @memoize_method
    def face_mass_matrix(self):
        face_vdm = self.face_vandermonde()

        return numpy.asarray(
                la.inv(
                    numpy.dot(face_vdm, face_vdm.T)),
                order="C")

    @memoize_method
    def face_affine_maps(self):
        """Return an affine map for each face that maps the (n-1)-dimensional
        face unit coordinates to their volume coordintes.
        """
        face_vertex_node_index_lists = \
                self.geometry.face_vertices(self.vertex_indices())
        from pytools import flatten, one
        vertex_node_indices = set(flatten(face_vertex_node_index_lists))

        def find_missing_node(face_vertex_node_indices):
            return unit_nodes[one(
                vertex_node_indices - set(face_vertex_node_indices))]

        unit_nodes = self.unit_nodes()
        sets_of_to_points = [[unit_nodes[fvni]
                for fvni in face_vertex_node_indices]
                + [find_missing_node(face_vertex_node_indices)]
                for face_vertex_node_indices in face_vertex_node_index_lists]
        from_points = sets_of_to_points[0]

        # Construct an affine map that promotes face nodes into volume
        # by appending -1, this should end up on the first face
        dim = self.dimensions
        from hedge.tools.affine import AffineMap
        from hedge.tools.linalg import unit_vector
        to_face_1 = AffineMap(
                numpy.vstack([
                    numpy.eye(dim-1, dtype=numpy.float64),
                    numpy.zeros(dim-1)]),
                -unit_vector(dim, dim-1, dtype=numpy.float64))

        def finish_affine_map(amap):
            return amap.post_compose(to_face_1)

        from hedge.tools.affine import identify_affine_map
        return [
                finish_affine_map(
                    identify_affine_map(from_points, to_points))
                for to_points in sets_of_to_points]

    # {{{ face matching
    def get_face_index_shuffle_lookup_map_for_nodes(self, face_nodes):
        first_face_vertex_node_index_lists = \
                self.geometry.face_vertices(self.vertex_indices())[0]

        def check_and_chop(pt):
            assert abs(pt[-1] - (-1)) < 1e-13
            return pt[:-1]

        unodes = self.unit_nodes()
        face_unit_vertices = [check_and_chop(unodes[i])
                for i in first_face_vertex_node_index_lists]

        class FaceIndexShuffle:
            def __init__(self, vert_perm, idx_map):
                self.vert_perm = vert_perm
                self.idx_map = idx_map

            def __hash__(self):
                return hash(self.vert_perm)

            def __eq__(self, other):
                return self.vert_perm == other.vert_perm

            def __call__(self, indices):
                return tuple(indices[i] for i in self.idx_map)

        result = {}

        from pytools import generate_unique_permutations
        for vert_perm in generate_unique_permutations(
                tuple(range(self.dimensions))):
            permuted_face_unit_vertices = [
                    face_unit_vertices[i] for i in vert_perm]

            from hedge.tools.affine import identify_affine_map
            amap = identify_affine_map(
                    face_unit_vertices, permuted_face_unit_vertices)

            from hedge.tools.indexing import find_index_map_from_node_sets
            imap = find_index_map_from_node_sets(
                    face_nodes, [amap(node) for node in face_nodes])

            result[vert_perm] = FaceIndexShuffle(vert_perm, imap)

        return result

    @memoize_method
    def get_face_index_shuffle_lookup_map(self):
        def check_and_chop(pt):
            assert abs(pt[-1] - (-1)) < 1e-13
            return pt[:-1]

        unodes = self.unit_nodes()
        face_unit_nodes = [
                check_and_chop(unodes[i])
                for i in self.face_indices()[0]]

        return self.get_face_index_shuffle_lookup_map_for_nodes(face_unit_nodes)

    def get_face_index_shuffle_backend(self, face_1_vertices, face_2_vertices,
            lookup_map):
        # First normalize face_2_vertices to 0,1,2,...
        idx_normalize_map = dict(
                (i_i, i) for i, i_i in enumerate(face_1_vertices))

        try:
            normalized_face_2_vertices = tuple(
                    idx_normalize_map[i] for i in face_2_vertices)
        except KeyError:
            # Assuming all vertex indices are distinct, then the
            # above lookup will already catch non-overlaps.
            # Hence we don't need to protect the lookup below.
            raise FaceVertexMismatch("face vertices do not match")

        # Then look them up in a hash table.
        return lookup_map[normalized_face_2_vertices]

    def get_face_index_shuffle_to_match(self, face_1_vertices, face_2_vertices):
        return self.get_face_index_shuffle_backend(
                face_1_vertices, face_2_vertices,
                self.get_face_index_shuffle_lookup_map())
    # }}}
    # }}}

    # {{{ node wrangling ------------------------------------------------------
    def equidistant_barycentric_nodes(self):
        """Generate equidistant nodes in barycentric coordinates."""
        for indices in self.node_tuples():
            divided = tuple(i / self.order for i in indices)
            yield (1 - sum(divided),) + divided

    def equidistant_equilateral_nodes(self):
        """Generate equidistant nodes in equilateral coordinates."""

        for bary in self.equidistant_barycentric_nodes():
            yield self.barycentric_to_equilateral(bary)

    def equidistant_unit_nodes(self):
        """Generate equidistant nodes in unit coordinates."""

        for bary in self.equidistant_barycentric_nodes():
            yield self.equilateral_to_unit(
                    self.barycentric_to_equilateral(bary))

    @memoize_method
    def unit_nodes(self):
        """Generate the warped nodes in unit coordinates (r,s,...)."""
        return [self.equilateral_to_unit(node)
                for node in self.equilateral_nodes()]

    # }}}

    # {{{ basis functions -----------------------------------------------------
    def generate_mode_identifiers(self):
        """Generate a hashable objects identifying each basis function,
        in order.

        The output from this function is required to be in the same order
        as that of L{basis_functions} and L{grad_basis_functions}, and thereby
        also from L{vandermonde}.
        """
        from pytools import \
                generate_nonnegative_integer_tuples_summing_to_at_most

        return generate_nonnegative_integer_tuples_summing_to_at_most(
                self.order, self.dimensions)

    # }}}

    # {{{ time step scaling ---------------------------------------------------
    def dt_non_geometric_factor(self):
        unodes = self.unit_nodes()
        vertex_indices = self.vertex_indices()
        return 2 / 3 * \
                min(min(min(
                    la.norm(unodes[face_node_index]-unodes[vertex_index])
                    for vertex_index in vertex_indices
                    if vertex_index != face_node_index)
                    for face_node_index in face_indices)
                    for face_indices in self.face_indices())

    # }}}

    # {{{ quadrature ----------------------------------------------------------
    class QuadratureInfo:
        def __init__(self, ldis, exact_to_degree):
            self.ldis = ldis
            self.exact_to_degree = exact_to_degree

            from hedge.quadrature import get_simplex_cubature
            v_quad = get_simplex_cubature(exact_to_degree, ldis.dimensions)
            self.volume_nodes = v_quad.points
            self.volume_weights = v_quad.weights

            f_quad = get_simplex_cubature(exact_to_degree, ldis.dimensions-1)
            self.face_nodes = f_quad.points
            self.face_weights = f_quad.weights

        def node_count(self):
            return len(self.volume_nodes)

        def face_count(self):
            return self.ldis.face_count()

        def face_node_count(self):
            return len(self.face_nodes)

        @memoize_method
        def face_indices(self):
            """Return a list of face index lists. Each face index list contains
            the local node numbers of the nodes on that face.

            Note: this relates to the facial DOF quadrature vector.
            """
            fnc = self.face_node_count()
            return [tuple(range(fnc*face_idx, fnc*(face_idx+1)))
                    for face_idx in range(self.ldis.face_count())]

        # {{{ matrices
        @memoize_method
        def vandermonde(self):
            from hedge.polynomial import generic_vandermonde
            return generic_vandermonde(
                    self.volume_nodes,
                    list(self.ldis.basis_functions()))

        @memoize_method
        def face_vandermonde(self):
            from hedge.polynomial import generic_vandermonde
            return generic_vandermonde(
                    self.face_nodes,
                    list(self.ldis.face_basis()))

        @memoize_method
        def volume_up_interpolation_matrix(self):
            from hedge.tools.linalg import leftsolve
            return numpy.asarray(
                    leftsolve(
                        self.ldis.vandermonde(),
                        self.vandermonde()),
                    order="C")

        @memoize_method
        def diff_vandermonde_matrices(self):
            from hedge.polynomial import generic_multi_vandermonde
            return generic_multi_vandermonde(
                    self.volume_nodes,
                    list(self.ldis.grad_basis_functions()))

        @memoize_method
        def volume_to_face_up_interpolation_matrix(self):
            """Generate a matrix that maps volume nodal values to
            a vector of face nodal values on the quadrature grid, with
            faces immediately concatenated, i.e::

                [face 1 nodal data][face 2 nodal data]...
            """
            ldis = self.ldis

            face_maps = ldis.face_affine_maps()

            from pytools import flatten
            face_nodes = list(flatten(
                    [face_map(qnode) for qnode in self.face_nodes]
                    for face_map in face_maps))

            from hedge.polynomial import generic_vandermonde
            vdm = generic_vandermonde(
                    face_nodes,
                    list(ldis.basis_functions()))

            from hedge.tools.linalg import leftsolve
            return leftsolve(self.ldis.vandermonde(), vdm)

        @memoize_method
        def face_up_interpolation_matrix(self):
            from hedge.tools.linalg import leftsolve
            return leftsolve(
                        self.ldis.face_vandermonde(),
                        self.face_vandermonde())

        @memoize_method
        def mass_matrix(self):
            return numpy.asarray(
                    la.solve(
                        self.ldis.vandermonde().T,
                        numpy.dot(
                            self.vandermonde().T,
                            numpy.diag(self.volume_weights))),
                    order="C")

        @memoize_method
        def stiffness_t_matrices(self):
            return [numpy.asarray(
                la.solve(
                    self.ldis.vandermonde().T,
                    numpy.dot(
                        diff_vdm.T,
                        numpy.diag(self.volume_weights))),
                    order="C")
                    for diff_vdm in self.diff_vandermonde_matrices()]

        @memoize_method
        def face_mass_matrix(self):
            return numpy.asarray(
                    la.solve(
                        self.ldis.face_vandermonde().T,
                        numpy.dot(
                            self.face_vandermonde().T,
                            numpy.diag(self.face_weights))),
                    order="C")

        @memoize_method
        def multi_face_mass_matrix(self):
            z = self.ldis._assemble_multi_face_mass_matrix(
                    self.face_mass_matrix())
            return z
        # }}}

        # {{{ face matching
        @memoize_method
        def get_face_index_shuffle_lookup_map(self):
            return self.ldis.get_face_index_shuffle_lookup_map_for_nodes(
                    self.face_nodes)

        def get_face_index_shuffle_to_match(self, face_1_vertices, face_2_vertices):
            return self.ldis.get_face_index_shuffle_backend(
                    face_1_vertices, face_2_vertices,
                    self.get_face_index_shuffle_lookup_map())
        # }}}

    @memoize_method
    def get_quadrature_info(self, exact_to_degree):
        return self.QuadratureInfo(self, exact_to_degree)

    # }}}

# }}}
# }}}


# {{{ interval local discretization -------------------------------------------
class IntervalDiscretization(PkSimplexDiscretization):
    """An arbitrary-order polynomial finite interval element.

    Coordinate systems used:
    ========================

    unit coordinates (r)::

        ---[--------0--------]--->
           -1                1
    """

    dimensions = 1
    has_local_jacobians = False
    geometry = hedge.mesh.element.Interval

    def __init__(self, order):
        self.order = order

    # numbering ---------------------------------------------------------------
    @memoize_method
    def node_tuples(self):
        """Generate tuples enumerating the node indices present
        in this element. Each tuple has a length equal to the dimension
        of the element. The tuples constituents are non-negative integers
        whose sum is less than or equal to the order of the element.

        The order in which these nodes are generated dictates the local
        node numbering.
        """
        return [(i,) for i in range(self.order + 1)]

    def faces_for_node_tuple(self, node_idx):
        """Return the list of face indices of faces on which the node
        represented by *node_idx* lies.
        """

        if node_idx == (0,):
            if self.order == 0:
                return [0, 1]
            else:
                return [0]
        elif node_idx == (self.order,):
            return [1]
        else:
            return []

    # node wrangling ----------------------------------------------------------
    def nodes(self):
        """Generate warped nodes in unit coordinates (r,)."""

        if self.order == 0:
            return [numpy.array([0.5])]
        else:
            from hedge.quadrature import legendre_gauss_lobatto_points
            return [numpy.array([x])
                    for x in legendre_gauss_lobatto_points(self.order)]

    equilateral_nodes = nodes
    unit_nodes = nodes

    @staticmethod
    def barycentric_to_equilateral((lambda1, lambda2)):
        """Return the equilateral (x,y) coordinate corresponding
        to the barycentric coordinates (lambda1..lambdaN)."""

        # reflects vertices in equilateral coordinates
        return numpy.array([2*lambda1-1], dtype=numpy.float64)

    equilateral_to_unit = AffineMap(
            numpy.array([[1]], dtype=numpy.float64),
            numpy.array([0], dtype=numpy.float64))

    unit_to_barycentric = AffineMap(
            numpy.array([[1/2], [-1/2]], dtype=numpy.float64),
            numpy.array([1/2, 1/2]))

    @memoize_method
    def get_submesh_indices(self):
        """Return a list of tuples of indices into the node list that
        generate a tesselation of the reference element."""

        return [(i, i + 1) for i in range(self.order)]

    # {{{ basis functions -----------------------------------------------------
    @memoize_method
    def basis_functions(self):
        """Get a sequence of functions that form a basis of the approximation
        space.

        The approximation space is spanned by the polynomials:::

          r**i for i <= N
        """
        from hedge.polynomial import VectorLegendreFunction
        return [VectorLegendreFunction(idx[0])
                for idx in self.generate_mode_identifiers()]

    def grad_basis_functions(self):
        """Get the gradient functions of the basis_functions(), in the
        same order.
        """

        from hedge.polynomial import DiffLegendreFunction

        class DiffVectorLF:
            def __init__(self, n):
                self.dlf = DiffLegendreFunction(n)

            def __call__(self, x):
                return numpy.array([self.dlf(x[0])])

        return [DiffVectorLF(idx[0])
            for idx in self.generate_mode_identifiers()]

    def face_basis(self):
        return [lambda x: 1]

    # }}}

    # time step scaling -------------------------------------------------------
    def dt_non_geometric_factor(self):
        if self.order == 0:
            return 1
        else:
            unodes = self.unit_nodes()
            return la.norm(unodes[0] - unodes[1]) * 0.85

    def dt_geometric_factor(self, vertices, el):
        return abs(el.map.jacobian())

# }}}


# {{{ triangle local discretization -------------------------------------------
class TriangleDiscretization(PkSimplexDiscretization):
    """An arbitrary-order triangular finite element.

    Coordinate systems used:
    ========================

    unit coordinates (r,s)::

    C
    |\\
    | \\
    |  O
    |   \\
    |    \\
    A-----B

    Points in unit coordinates::

        O = (0,0)
        A = (-1,-1)
        B = (1,-1)
        C = (-1,1)

    equilateral coordinates (x,y)::

            C
           / \\
          /   \\
         /     \\
        /   O   \\
       /         \\
      A-----------B

    Points in equilateral coordinates::

        O = (0,0)
        A = (-1,-1/sqrt(3))
        B = (1,-1/sqrt(3))
        C = (0,2/sqrt(3))

    When global vertices are passed in, they are mapped to the
    reference vertices A, B, C in order.

    Faces are always ordered AB, BC, AC.
    """

    # In case you were wondering: the double backslashes in the docstring
    # are required because single backslashes only escape their subsequent
    # newlines, and thus end up not yielding a correct docstring.

    dimensions = 2
    has_local_jacobians = False
    geometry = hedge.mesh.element.Triangle

    def __init__(self, order, fancy_node_ordering=False):
        self.order = order
        self.fancy_node_ordering = fancy_node_ordering

    # numbering ---------------------------------------------------------------
    @memoize_method
    def node_tuples(self):
        """Generate tuples enumerating the node indices present
        in this element. Each tuple has a length equal to the dimension
        of the element. The tuples constituents are non-negative integers
        whose sum is less than or equal to the order of the element.

        The order in which these nodes are generated dictates the local
        node numbering.
        """
        from pytools import \
                generate_nonnegative_integer_tuples_summing_to_at_most
        node_tups = list(
                generate_nonnegative_integer_tuples_summing_to_at_most(
                    self.order, self.dimensions))

        if not self.fancy_node_ordering:
            return node_tups

        faces_to_nodes = {}
        for node_tup in node_tups:
            faces_to_nodes.setdefault(
                    frozenset(self.faces_for_node_tuple(node_tup)),
                    []).append(node_tup)

        result = []

        def add_face_nodes(faces):
            result.extend(faces_to_nodes.get(frozenset(faces), []))

        add_face_nodes([])
        add_face_nodes([0])
        add_face_nodes([0, 1])
        add_face_nodes([1])
        add_face_nodes([1, 2])
        add_face_nodes([2])
        add_face_nodes([0, 2])

        assert set(result) == set(node_tups)
        assert len(result) == len(node_tups)

        return result

    def faces_for_node_tuple(self, node_tuple):
        """Return the list of face indices of faces on which the node
        represented by *node_tuple* lies.
        """
        m, n = node_tuple

        result = []
        if n == 0:
            result.append(0)
        if n + m == self.order:
            result.append(1)
        if m == 0:
            result.append(2)
        return result

    # node wrangling ----------------------------------------------------------
    @staticmethod
    def barycentric_to_equilateral((lambda1, lambda2, lambda3)):
        """Return the equilateral (x,y) coordinate corresponding
        to the barycentric coordinates (lambda1..lambdaN)."""

        # reflects vertices in equilateral coordinates
        return numpy.array([
            (- lambda1 + lambda2),
            (- lambda1 - lambda2 + 2 * lambda3) / sqrt(3.0)])

    # see doc/hedge-notes.tm
    equilateral_to_unit = AffineMap(
            numpy.array([[1, -1 / sqrt(3)], [0, 2 / sqrt(3)]]),
                numpy.array([-1/3, -1/3]))

    unit_to_barycentric = AffineMap(
            numpy.array([
                [1/2, 0],
                [0, 1/2],
                [-1/2, -1/2]], dtype=numpy.float64),
            numpy.array([1/2, 1/2, 0]))

    def equilateral_nodes(self):
        """Generate warped nodes in equilateral coordinates (x,y)."""

        # port of Warburton's Nodes2D routine
        # note that the order of the barycentric coordinates is changed
        # match the order of the equilateral vertices

        # Not much is left of the original routine--it was very redundant.
        # The test suite still contains the original code and verifies this
        # one against it.

        # Set optimized parameter alpha, depending on order N
        alpha_opt = [0.0000, 0.0000, 1.4152, 0.1001, 0.2751, 0.9800, 1.0999,
                1.2832, 1.3648, 1.4773, 1.4959, 1.5743, 1.5770, 1.6223, 1.6258]

        try:
            alpha = alpha_opt[self.order-1]
        except IndexError:
            alpha = 5/3

        warp = TriangleWarper(alpha, self.order)

        for bp in self.equidistant_barycentric_nodes():
            yield self.barycentric_to_equilateral(bp) + warp(bp)

    @memoize_method
    def get_submesh_indices(self):
        """Return a list of tuples of indices into the node list that
        generate a tesselation of the reference element."""

        node_dict = dict(
                (ituple, idx)
                for idx, ituple in enumerate(self.node_tuples()))

        result = []
        for i, j in self.node_tuples():
            if i + j < self.order:
                result.append(
                        (node_dict[i, j], node_dict[i + 1, j],
                            node_dict[i, j+1]))
            if i + j < self.order-1:
                result.append(
                    (node_dict[i + 1, j+1], node_dict[i, j + 1],
                        node_dict[i + 1, j]))
        return result

    # {{{ basis functions -----------------------------------------------------
    @memoize_method
    def basis_functions(self):
        """Get a sequence of functions that form a basis of the
        approximation space.

        The approximation space is spanned by the polynomials:::

          r**i * s**j for i+j <= N
        """
        return [TriangleBasisFunction(*idx) for idx in
                self.generate_mode_identifiers()]

    def grad_basis_functions(self):
        """Get the gradient functions of the basis_functions(),
        in the same order.
        """

        return [GradTriangleBasisFunction(*idx) for idx in
                self.generate_mode_identifiers()]

    @memoize_method
    def face_basis(self):
        from hedge.polynomial import VectorLegendreFunction
        return [VectorLegendreFunction(i) for i in range(self.order+1)]
    # }}}

    # time step scaling -------------------------------------------------------
    def dt_geometric_factor(self, vertices, el):
        area = abs(2 * el.map.jacobian())
        semiperimeter = sum(la.norm(vertices[vi1] - vertices[vi2])
                for vi1, vi2 in [(0, 1), (1, 2), (2, 0)])/2
        return area / semiperimeter

# }}}


# {{{ tetrahedron local discretization ----------------------------------------
class TetrahedronDiscretization(PkSimplexDiscretization):
    """An arbitrary-order tetrahedral finite element.

    Coordinate systems used:
    ========================

    unit coordinates (r,s,t)::

               ^ s
               |
               C
              /|\\
             / | \\
            /  |  \\
           /   |   \\
          /   O|    \\
         /   __A-----B---> r
        /_--^ ___--^^
       ,D--^^^
    t L

    (squint, and it might start making sense...)

    Points in unit coordinates::

        O=( 0, 0, 0)
        A=(-1,-1,-1)
        B=(+1,-1,-1)
        C=(-1,+1,-1)
        D=(-1,-1,+1)

    Points in equilateral coordinates (x,y,z)::

        O = (0,0,0)
        A = (-1,-1/sqrt(3),-1/sqrt(6))
        B = ( 1,-1/sqrt(3),-1/sqrt(6))
        C = ( 0, 2/sqrt(3),-1/sqrt(6))
        D = ( 0,         0, 3/sqrt(6))

    When global vertices are passed in, they are mapped to the
    reference vertices A, B, C, D in order.

    Faces are ordered ABC, ABD, ACD, BCD.
    """

    # In case you were wondering: the double backslashes in the docstring
    # above are required because single backslashes serve to escape
    # their subsequent newlines, and thus end up not yielding a
    # correct docstring.

    dimensions = 3
    has_local_jacobians = False
    geometry = hedge.mesh.element.Tetrahedron

    def __init__(self, order):
        self.order = order

    # numbering ---------------------------------------------------------------
    @memoize_method
    def node_tuples(self):
        """Generate tuples enumerating the node indices present
        in this element. Each tuple has a length equal to the dimension
        of the element. The tuples constituents are non-negative integers
        whose sum is less than or equal to the order of the element.

        The order in which these nodes are generated dictates the local
        node numbering.
        """
        from pytools import \
                generate_nonnegative_integer_tuples_summing_to_at_most
        node_tups = list(
                generate_nonnegative_integer_tuples_summing_to_at_most(
                    self.order, self.dimensions))

        if False:
            # hand-tuned node order
            faces_to_nodes = {}
            for node_tup in node_tups:
                faces_to_nodes.setdefault(
                        frozenset(self.faces_for_node_tuple(node_tup)),
                        []).append(node_tup)

            result = []

            def add_face_nodes(faces):
                result.extend(faces_to_nodes.get(frozenset(faces), []))

            add_face_nodes([0, 3])
            add_face_nodes([0])
            add_face_nodes([0, 2])
            add_face_nodes([0, 1])
            add_face_nodes([0, 1, 2])
            add_face_nodes([0, 1, 3])
            add_face_nodes([0, 2, 3])
            add_face_nodes([1])
            add_face_nodes([1, 2])
            add_face_nodes([1, 2, 3])
            add_face_nodes([1, 3])
            add_face_nodes([2])
            add_face_nodes([2, 3])
            add_face_nodes([3])
            add_face_nodes([])

            assert set(result) == set(node_tups)
            assert len(result) == len(node_tups)

        if True:
            # average-sort heuristic node order
            from pytools import average

            def order_number_for_node_tuple(nt):
                faces = self.faces_for_node_tuple(nt)
                if not faces:
                    return -1
                elif len(faces) >= 3:
                    return 1000
                else:
                    return average(faces)

            def cmp_node_tuples(nt1, nt2):
                return cmp(
                        order_number_for_node_tuple(nt1),
                        order_number_for_node_tuple(nt2))

            result = node_tups
            #result.sort(cmp_node_tuples)

        #for i, nt in enumerate(result):
            #fnt = self.faces_for_node_tuple(nt)
            #print i, nt, fnt

        return result

    def faces_for_node_tuple(self, node_tuple):
        """Return the list of face indices of faces on which the node
        represented by *node_tuple* lies.
        """
        m, n, o = node_tuple
        result = []

        if o == 0:
            result.append(0)
        if n == 0:
            result.append(1)
        if m == 0:
            result.append(2)
        if n + m + o == self.order:
            result.append(3)

        return result

    # {{{ node wrangling ------------------------------------------------------
    @staticmethod
    def barycentric_to_equilateral((lambda1, lambda2, lambda3, lambda4)):
        """Return the equilateral (x,y) coordinate corresponding
        to the barycentric coordinates (lambda1..lambdaN)."""

        # reflects vertices in equilateral coordinates
        return numpy.array([
            (-lambda1 + lambda2),
            (-lambda1 - lambda2 + 2*lambda3)/sqrt(3.0),
            (-lambda1 - lambda2 - lambda3 + 3 * lambda4)/sqrt(6.0),
            ])

    # see doc/hedge-notes.tm
    equilateral_to_unit = AffineMap(
            numpy.array([
                [1, -1/sqrt(3), -1/sqrt(6)],
                [0,  2/sqrt(3), -1/sqrt(6)],
                [0,         0,  sqrt(6)/2]
                ]),
                numpy.array([-1/2, -1/2, -1/2]))

    unit_to_barycentric = AffineMap(
            numpy.array([
                [1/2, 0, 0],
                [0, 1/2, 0],
                [0, 0, 1/2],
                [-1/2, -1/2, -1/2]], dtype=numpy.float64),
            numpy.array([1/2, 1/2, -1/2]))

    def equilateral_nodes(self):
        """Generate warped nodes in equilateral coordinates (x,y)."""

        # port of Hesthaven/Warburton's Nodes3D routine

        # Set optimized parameter alpha, depending on order N
        alpha_opt = [0, 0, 0, 0.1002, 1.1332, 1.5608, 1.3413, 1.2577, 1.1603,
                1.10153, 0.6080, 0.4523, 0.8856, 0.8717, 0.9655]
        if self.order-1 < len(alpha_opt):
            alpha = alpha_opt[self.order-1]
        else:
            alpha = 1

        from pytools import wandering_element

        vertices = [self.barycentric_to_equilateral(bary)
                for bary in wandering_element(self.dimensions + 1)]
        all_vertex_indices = range(self.dimensions + 1)
        face_vertex_indices = self.geometry \
                .face_vertices(all_vertex_indices)
        faces_vertices = self.geometry \
                .face_vertices(vertices)

        bary_points = list(self.equidistant_barycentric_nodes())
        equi_points = [self.barycentric_to_equilateral(bp)
                for bp in bary_points]

        from hedge.tools import normalize
        from operator import add, mul

        tri_warp = TriangleWarper(alpha, self.order)

        for fvi, (v1, v2, v3) in zip(face_vertex_indices, faces_vertices):
            # find directions spanning the face: "base" and "altitude"
            directions = [normalize(v2 - v1), normalize((v3)-(v1+v2)/2)]

            # the two should be orthogonal
            assert abs(numpy.dot(directions[0], directions[1])) < 1e-16

            # find the vertex opposite to the current face
            opp_vertex_index = (
                    set(all_vertex_indices)
                    - set(fvi)).__iter__().next()

            shifted = []
            for bp, ep in zip(bary_points, equi_points):
                face_bp = [bp[i] for i in fvi]

                blend = reduce(mul, face_bp) * (1+alpha*bp[opp_vertex_index])**2

                for i in fvi:
                    denom = bp[i] + 0.5*bp[opp_vertex_index]
                    if abs(denom) > 1e-12:
                        blend /= denom
                    else:
                        blend = 0.5  # each edge gets shifted twice
                        break

                shifted.append(ep + blend*reduce(add,
                    (tw*dir for tw, dir in zip(tri_warp(face_bp), directions))))

            equi_points = shifted

        return equi_points

    @memoize_method
    def get_submesh_indices(self):
        """Return a list of tuples of indices into the node list that
        generate a tesselation of the reference element."""

        node_dict = dict(
                (ituple, idx)
                for idx, ituple in enumerate(self.node_tuples()))

        def add_tuples(a, b):
            return tuple(ac+bc for ac, bc in zip(a, b))

        def try_add_tet(d1, d2, d3, d4):
            try:
                result.append((
                    node_dict[add_tuples(current, d1)],
                    node_dict[add_tuples(current, d2)],
                    node_dict[add_tuples(current, d3)],
                    node_dict[add_tuples(current, d4)],
                    ))
            except KeyError:
                pass

        result = []
        for current in self.node_tuples():
            # this is a tesselation of a cube into six tets.
            # subtets that fall outside of the master tet are simply not added.

            # positively oriented
            try_add_tet((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))
            try_add_tet((1, 0, 1), (1, 0, 0), (0, 0, 1), (0, 1, 0))
            try_add_tet((1, 0, 1), (0, 1, 1), (0, 1, 0), (0, 0, 1))

            try_add_tet((1, 0, 0), (0, 1, 0), (1, 0, 1), (1, 1, 0))
            try_add_tet((0, 1, 1), (0, 1, 0), (1, 1, 0), (1, 0, 1))
            try_add_tet((0, 1, 1), (1, 1, 1), (1, 0, 1), (1, 1, 0))

        return result

    # }}}

    # basis functions ---------------------------------------------------------
    @memoize_method
    def basis_functions(self):
        """Get a sequence of functions that form a basis of the approximation space.

        The approximation space is spanned by the polynomials::

          r**i * s**j * t**k  for  i+j+k <= order
        """
        return [TetrahedronBasisFunction(*idx) for idx in
                self.generate_mode_identifiers()]

    def grad_basis_functions(self):
        """Get the (r,s,...) gradient functions of the basis_functions(),
        in the same order.
        """
        return [GradTetrahedronBasisFunction(*idx) for idx in
                self.generate_mode_identifiers()]

    @memoize_method
    def face_basis(self):
        from pytools import generate_nonnegative_integer_tuples_summing_to_at_most

        return [TriangleBasisFunction(*mode_tup)
                for mode_tup in
                generate_nonnegative_integer_tuples_summing_to_at_most(
                    self.order, self.dimensions-1)]

    # time step scaling -------------------------------------------------------
    def dt_geometric_factor(self, vertices, el):
        result = abs(el.map.jacobian())/max(abs(fj) for fj in el.face_jacobians)
        if self.order in [1, 2]:
            from warnings import warn
            warn("cowardly halving timestep for order 1 and 2 tets "
                    "to avoid CFL issues")
            result /= 2

        return result

# }}}


GEOMETRY_TO_LDIS = {
        hedge.mesh.element.Interval: IntervalDiscretization,
        hedge.mesh.element.Triangle: TriangleDiscretization,
        hedge.mesh.element.CurvedTriangle: TriangleDiscretization,
        hedge.mesh.element.Tetrahedron: TetrahedronDiscretization,
        hedge.mesh.element.CurvedTetrahedron: TetrahedronDiscretization,
        }




# vim: foldmethod=marker
