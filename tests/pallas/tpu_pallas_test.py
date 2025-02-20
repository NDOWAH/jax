# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test TPU-specific extensions to pallas_call."""

import contextlib
import functools
import io
import re
import sys
from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import lax
from jax._src import checkify
from jax._src import state
from jax._src import test_util as jtu
from jax._src.interpreters import partial_eval as pe
from jax._src.lib import xla_extension
from jax._src.pallas.pallas_call import _trace_to_jaxpr
from jax.experimental import mesh_utils
from jax.experimental import mosaic
from jax.experimental import pallas as pl
from jax.experimental import shard_map
from jax.experimental.pallas import tpu as pltpu
from jax.experimental.pallas.ops.tpu import example_kernel
from jax.extend import linear_util as lu
import jax.numpy as jnp
import numpy as np


jax.config.parse_flags_with_absl()

P = jax.sharding.PartitionSpec

partial = functools.partial


@contextlib.contextmanager
def string_stdout():
  """Redirects stdout to a string."""
  initial_stdout = sys.stdout
  stringio = io.StringIO()
  sys.stdout = stringio
  yield stringio
  sys.stdout = initial_stdout


class PallasBaseTest(jtu.JaxTestCase):
  INTERPRET: bool = False

  def setUp(self):
    if not jtu.test_device_matches(['tpu']) and not self.INTERPRET:
      self.skipTest('Test requires TPUs, or interpret mode')
    super().setUp()
    _trace_to_jaxpr.cache_clear()

  def pallas_call(self, *args, **kwargs):
    return pl.pallas_call(*args, **kwargs, interpret=self.INTERPRET)


class PallasCallScalarPrefetchTest(PallasBaseTest):

  def test_trivial_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[
                pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
            ],
            out_specs=pl.BlockSpec(
                (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
            ),
            grid=8,
        ),
    )(s, x)
    np.testing.assert_allclose(out, x.reshape((8, 8, -1))[s].reshape(x.shape))

  def test_trivial_scalar_prefetch_with_windowless_args(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
        ),
    )(s, x)
    np.testing.assert_array_equal(out, x)

  def test_block_spec_with_wrong_block_shape_errors(self):
    def body(x_ref, o_ref):
      o_ref[...] = x_ref[...]

    x = jnp.ones((16, 128))
    with self.assertRaisesRegex(
        ValueError,
        'Block shape .* must have the same number of dimensions as the array shape .*'):
      _ = self.pallas_call(
          body,
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=0,
              in_specs=[pl.BlockSpec((128,), lambda i: (i, 0))],  # WRONG
              out_specs=pl.BlockSpec((8, 128,), lambda i: (i, 0)),
              grid=(2,),
          ),
          out_shape=x,
      )(x)

  def test_block_spec_with_index_map_that_accepts_wrong_number_of_args_errors(self):
    def body(x_ref, o_ref):
      o_ref[...] = x_ref[...]

    x = jnp.ones((16, 128))
    with self.assertRaisesRegex(
        TypeError,
        'missing 1 required positional argument: \'j\''):
      _ = self.pallas_call(
          body,
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=0,
              in_specs=[pl.BlockSpec((8, 128,), lambda i, j: (i, 0))],  # WRONG
              out_specs=pl.BlockSpec((8, 128,), lambda i: (i, 0),),
              grid=(2,),
          ),
          out_shape=x,
      )(x)

  def test_block_spec_with_index_map_returns_wrong_number_of_values_errors(self):
    def body(x_ref, o_ref):
      o_ref[...] = x_ref[...]

    x = jnp.ones((16, 128))
    with self.assertRaisesRegex(
        ValueError,
        r'Index map for input\[0\] must return 2 values to match block_shape=\(8, 128\).'
        ' Currently returning 1 values.'):
      _ = self.pallas_call(
          body,
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=0,
              in_specs=[pl.BlockSpec((8, 128,), lambda i: (i,))],  # WRONG
              out_specs=pl.BlockSpec((8, 128), lambda i: (i, 0)),
              grid=(2,),
          ),
          out_shape=x,
      )(x)

  def test_vmap_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(2 * 8 * 8 * 128, dtype=jnp.int32).reshape((2, 8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    def f(x):
      return self.pallas_call(
          body,
          out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
              ],
              out_specs=pl.BlockSpec(
                  (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
              ),
              grid=8),
      )(s, x)
    np.testing.assert_allclose(
        jax.vmap(f)(x), x.reshape((2, 8, 8, -1))[:, s].reshape(x.shape)
    )

  def test_multiple_scalar_prefetch(self):
    def body(s1_ref, s2_ref, x_ref, o_ref):
      del s1_ref, s2_ref
      o_ref[...] = x_ref[...]

    s1 = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    s2 = jnp.array([7, 6, 5, 4, 3, 2, 1, 0], jnp.int32)
    x = jnp.arange(64 * 128, dtype=jnp.int32).reshape((64, 128))

    def _x_transform(i, s1_ref, _):
      return s1_ref[i], 0

    def _o_transform(i, _, s2_ref):
      return s2_ref[i], 0

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((64, 128), jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=2,
            in_specs=[
                pl.BlockSpec((8, 128), _x_transform),
            ],
            out_specs=pl.BlockSpec((8, 128), _o_transform),
            grid=8,
        ),
    )(s1, s2, x)
    out_ref = x.reshape((8, 8, -1))[s1][::-1].reshape((64, 128))
    np.testing.assert_allclose(out, out_ref)

  def test_scalar_interpreter(self):
    program = jnp.array([0, 0, 1, 0, 1, 1], jnp.int32)
    x = jnp.arange(8 * 8 * 128.0, dtype=jnp.float32).reshape(8 * 8, 128)

    def body(sprogram_ref, x_ref, o_ref, state_ref):
      x = x_ref[...]

      def add_branch_fn(j):
        state_ref[...] += jnp.float32(j)
        return ()

      def mult_branch_fn(j):
        state_ref[...] *= jnp.float32(j)
        return ()

      def single_inst(i, _):
        _ = jax.lax.switch(
            sprogram_ref[i],
            (
                add_branch_fn,
                mult_branch_fn,
            ),
            i,
        )

      # We can't use for loop state right now, because Pallas functionalizes it,
      # and Mosaic support for returning values form scf.if is incomplete.
      state_ref[...] = x
      lax.fori_loop(0, sprogram_ref.shape[0], single_inst, None, unroll=True)
      o_ref[...] = state_ref[...]

    # Ignore the scratch output.
    out, _ = self.pallas_call(
        body,
        out_shape=[
            jax.ShapeDtypeStruct(x.shape, jnp.float32),
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
        ],
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[pl.BlockSpec((8, 128), lambda i, *_: (i, 0))],
            out_specs=[
                pl.BlockSpec((8, 128), lambda i, *_: (i, 0)),
                pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            ],
            grid=8,
        ),
        debug=False,
    )(program, x)

    expected = x
    for i, p in enumerate(program):
      if p == 0:
        expected += i
      elif p == 1:
        expected *= i

    np.testing.assert_allclose(out, expected)

  def test_scalar_interpreter_dynamic_loop(self):
    loop_end = jnp.array([5], jnp.int32)

    def body(loop_end_ref, out_ref):
      out_ref[...] = jnp.zeros_like(out_ref)

      def loop_body(i, carry):
        del i, carry
        out_ref[...] += 1

      lax.fori_loop(0, loop_end_ref[0], loop_body, None)

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            out_specs=pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            grid=1,
        ),
    )(loop_end)

    expected_out = jnp.ones((8, 128), jnp.float32) * 5
    np.testing.assert_allclose(out, expected_out)

  def test_vmap_scalar_prefetch_1sized(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    s = s[None]
    x = x[None]

    out = jax.vmap(
        self.pallas_call(
            body,
            out_shape=jax.ShapeDtypeStruct(x.shape[1:], x.dtype),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=1,
                in_specs=[
                    pl.BlockSpec((x.shape[1] // 8, x.shape[2]), _x_transform),
                ],
                out_specs=pl.BlockSpec(
                    (x.shape[1] // 8, x.shape[2]), lambda i, _: (i, 0)
                ),
                grid=8,
            ),
        )
    )(s, x)
    np.testing.assert_allclose(
        out, x.reshape((1, 8, 8, -1))[:, s].reshape(x.shape)
    )

  def test_nontrivial_vmap_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(2 * 8 * 8 * 128, dtype=jnp.int32).reshape((2, 8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    s = jnp.tile(s[None], [2, 1])

    @jax.jit
    @jax.vmap
    def kernel(s, x):
      return self.pallas_call(
          body,
          out_shape=jax.ShapeDtypeStruct(x.shape, x.dtype),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
              ],
              out_specs=pl.BlockSpec(
                  (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
              ),
              grid=8,
          ),
          compiler_params=dict(mosaic=dict(allow_input_fusion=[False, True])),
      )(s, x)

    first = x[0, ...].reshape((1, 8, 8, -1))[:, s[0, ...]].reshape(x.shape[1:])
    second = x[1, ...].reshape((1, 8, 8, -1))[:, s[1, ...]].reshape(x.shape[1:])

    expected = jnp.stack([first, second])
    np.testing.assert_allclose(kernel(s, x), expected)

  def test_input_output_aliasing_with_scalar_prefetch(self):
    x = jnp.ones((32, 1024, 1024))
    expected = x + 1

    def kernel(_, x_ref, y_ref):
      y_ref[...] = x_ref[...] + 1.
    @partial(jax.jit, donate_argnums=(0,))
    def f(x):
      return self.pallas_call(
          kernel,
          out_shape=x,
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((None, 1024, 1024), lambda i, _: (i, 0, 0))
              ],
              out_specs=pl.BlockSpec(
                  (None, 1024, 1024), lambda i, _: (i, 0, 0)
              ),
              grid=(x.shape[0],),
          ),
          input_output_aliases={1: 0},
      )(jnp.array([1, 2, 3]), x)
    o = f(x)
    np.testing.assert_array_equal(o, expected)
    compiled = f.lower(jax.ShapeDtypeStruct(x.shape, x.dtype)).compile()
    mem_analysis = compiled.memory_analysis()
    expected_num_bytes = np.prod(x.shape) * x.dtype.itemsize
    self.assertEqual(mem_analysis.alias_size_in_bytes, expected_num_bytes)


class PallasCallScalarPrefetchInterpreterTest(PallasCallScalarPrefetchTest):
  INTERPRET: bool = True


class PallasCallDynamicGridTest(PallasBaseTest):

  def test_can_query_grid_statically_via_num_programs(self):

    def kernel(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int)
      self.assertEqual(num_programs, 2)

    self.pallas_call(kernel, out_shape=None, grid=(2,))()

  def test_can_query_grid_statically_via_num_programs_in_block_spec(self):

    def kernel(*_):
      pass

    def x_index_map(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int)
      self.assertEqual(num_programs, 2)
      return 0, 0
    self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec((8, 128), x_index_map)],
        out_shape=None,
        grid=(2,),
    )(jnp.ones((8, 128)))

  def test_dynamic_grid_has_dynamic_size(self):

    def kernel(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int, msg=type(num_programs))
      self.assertEqual(num_programs, 2)
      num_programs = pl.num_programs(1)
      self.assertIsInstance(num_programs, jax.Array)

    @jax.jit
    def outer(x):
      self.pallas_call(kernel, out_shape=None, grid=(2, x))()
    outer(2)

  def test_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )()
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 8.0, np.float32)
    )

  def test_dynamic_grid_overflow(self):
    # If we pad statically the dynamic grid dims to max int32, then the product
    # of this grid size will overflow int64 and can cause failing checks in XLA.
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(sum(pl.program_id(i) for i in range(3)) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2, steps + 1, 3),
          out_specs=pl.BlockSpec(shape, lambda *_: (0, 0)),
          out_shape=result_ty,
      )()
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 120.0, np.float32)
    )

  # TODO(apaszke): Add tests for scalar_prefetch too
  def test_dynamic_grid_scalar_input(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(scalar_input_ref, output_ref):
      output_ref[...] = jnp.full_like(output_ref, scalar_input_ref[0, 0])

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          out_shape=result_ty,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          grid=(steps * 2,),
      )(jnp.array([[42]], dtype=jnp.int32))

    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 42.0, np.float32)
    )

  def test_vmap_trivial_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(x_ref, y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = x_ref[...]
      y_ref[...] += 1

    @jax.jit
    @jax.vmap
    def dynamic_kernel(steps, x):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          in_specs=[pl.BlockSpec(shape, lambda i: (0, 0))],
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )(x)
    x = jnp.arange(8 * 128., dtype=jnp.float32).reshape((1, *shape))
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.array([4], jnp.int32), x), x + 8.0
    )

  def test_vmap_nontrivial_dynamic_grid(self):
    # Dynamic grid doesn't support vmapping over multiple distinct grid values
    # at the moment.
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    @jax.vmap
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )()
    out = dynamic_kernel(jnp.array([4, 8], jnp.int32))
    first = jnp.full(shape, fill_value=8.0, dtype=jnp.float32)
    second = jnp.full(shape, fill_value=16.0, dtype=jnp.float32)
    expected_out = jnp.stack([first, second], axis=0)
    np.testing.assert_array_equal(out, expected_out)

  def test_vmap_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(x_ref, y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = x_ref[...]
      y_ref[...] += jnp.float32(1.)

    @jax.jit
    def dynamic_kernel(x, steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )(x)
    x = jnp.arange(4 * 8 * 128., dtype=jnp.float32).reshape((4, *shape))
    np.testing.assert_array_equal(
        jax.jit(jax.vmap(dynamic_kernel, in_axes=(0, None)))(x, jnp.int32(4)),
        x + 8,
    )

  def test_num_programs(self):
    def kernel(y_ref):
      y_ref[0, 0] = pl.num_programs(0)

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
          out_shape=jax.ShapeDtypeStruct((1, 1), jnp.int32),
      )()

    self.assertEqual(dynamic_kernel(np.int32(4)), 8)

  @parameterized.parameters(range(1, 4))
  def test_vmap_num_programs(self, num_vmaps):
    result_ty = jax.ShapeDtypeStruct((8, 128), jnp.int32)

    def kernel(y_ref):
      y_ref[...] = jnp.full_like(y_ref, pl.num_programs(0))

    kernel_call = self.pallas_call(
        kernel,
        grid=(8,),
        out_specs=pl.BlockSpec(result_ty.shape, lambda i: (0, 0)),
        out_shape=result_ty,
    )

    out_shape = (*(2 for _ in range(num_vmaps)), *result_ty.shape)
    f = kernel_call
    for _ in range(num_vmaps):
      f = lambda impl=f: jax.vmap(impl, axis_size=2)()
    out = jax.jit(f)()
    np.testing.assert_array_equal(out, np.full(out_shape, 8.0))

  def test_num_programs_block_spec(self):
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    @jax.jit
    def dynamic_kernel(steps, x):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          in_specs=[
              pl.BlockSpec(
                  (8, 128),
                  # Should always evaluate to (1, 0)
                  lambda i: (1 + 8 - pl.num_programs(0), 0),
              )
          ],
          out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.int32),
      )(x)

    x = np.arange(4 * 8 * 128., dtype=np.int32).reshape((4 * 8, 128))
    np.testing.assert_array_equal(dynamic_kernel(np.int32(4), x), x[8:16])


class PallasCallDynamicGridInterpreterTest(PallasCallDynamicGridTest):
  INTERPRET = True


class PallasCallDMATest(PallasBaseTest):

  def setUp(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

    super().setUp()

  def test_can_have_unspecified_memory_spaces(self):
    def kernel(x_ref, y_ref):
      # Just test whether things compile
      del x_ref, y_ref

    x = jnp.ones((8, 128), dtype=jnp.float32)
    y = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY)],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    jax.block_until_ready(y)

  def test_run_scoped_tracks_effects(self):
    def kernel(x_ref, y_ref):
      def body(temp_ref):
        temp_ref[...] = jnp.ones_like(temp_ref)
        x_ref[...] = 4 * y_ref[...] + temp_ref[...]

      pltpu.run_scoped(body, pltpu.VMEM((8,), jnp.float32))
      return []

    jaxpr, _, _, () = pe.trace_to_jaxpr_dynamic(
        lu.wrap_init(kernel),
        [
            state.shaped_array_ref((8,), jnp.float32),
            state.shaped_array_ref((8,), jnp.float32),
        ],
    )
    expected_effects = {state.ReadEffect(1), state.WriteEffect(0)}
    self.assertSetEqual(jaxpr.effects, expected_effects)

  def test_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.ones_like(x_ref)
        y_ref[...] = 4 * x_ref[...]

      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_nested_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.zeros_like(x_ref)
        def inner_body(z_ref):
          z_ref[...] = jnp.ones_like(z_ref)
          x_ref[...] = z_ref[...]
        pltpu.run_scoped(inner_body, pltpu.VMEM((8, 128), jnp.float32))
        y_ref[...] = 4 * x_ref[...]
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_can_allocate_semaphore(self):
    def kernel(y_ref):
      def body(sem1):
        pass
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_multiple_semaphores(self):
    def kernel(y_ref):
      def body(sem1, sem2):
        pass
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA,
                       pltpu.SemaphoreType.REGULAR)

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_semaphore_array(self):
    def kernel(y_ref):
      def body(dma_sems, sems):
        self.assertTupleEqual(dma_sems.shape, (4,))
        self.assertTupleEqual(sems.shape, (3,))
        self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
        self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((4,)),
                       pltpu.SemaphoreType.REGULAR((3,)))

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_scratch_semaphore_array(self):
    def kernel(y_ref, dma_sems, sems):
      self.assertTupleEqual(dma_sems.shape, (4,))
      self.assertTupleEqual(sems.shape, (3,))
      self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
      self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))

    # TODO(b/345534352): Add interpret support for REGULAR semaphore.
    jax.block_until_ready(
        self.pallas_call(
            kernel,
            out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                scratch_shapes=[
                    pltpu.SemaphoreType.DMA((4,)),
                    pltpu.SemaphoreType.REGULAR((3,)),
                ],
            ),
        )()
    )

  def test_can_wait_on_semaphore(self):
    def kernel(y_ref):
      def body(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR)
      def body2(sem):
        pltpu.semaphore_signal(sem, 2)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body2, pltpu.SemaphoreType.REGULAR)
      def body3(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body3, pltpu.SemaphoreType.REGULAR)

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array(self):
    def kernel(y_ref):
      def body(sems):
        pltpu.semaphore_signal(sems.at[0])
        pltpu.semaphore_wait(sems.at[0])

        pltpu.semaphore_signal(sems.at[1], 2)
        pltpu.semaphore_wait(sems.at[1])
        pltpu.semaphore_wait(sems.at[1])

        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR((3,)))

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array_with_dynamic_index(self):
    def kernel(y_ref):
      i = pl.program_id(0)
      def body(sems):
        pltpu.semaphore_signal(sems.at[i, 0])
        pltpu.semaphore_wait(sems.at[i, 0])

        pltpu.semaphore_signal(sems.at[i, 1], 2)
        pltpu.semaphore_wait(sems.at[i, 1])
        pltpu.semaphore_wait(sems.at[i, 1])

        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR((4, 3)))

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    jax.block_until_ready(
        pl.pallas_call(
            kernel,
            in_specs=[],
            out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
            out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
            grid=4,
            debug=True,
        )()
    )

  def test_can_read_semaphore(self):
    m, n = 2, 3

    def kernel(y_ref):
      def body(sems):
        for r in range(m):
          for c in range(n):
            v = r * n + c
            pltpu.semaphore_signal(sems.at[r, c],v)
            y_ref[r, c] = pltpu.semaphore_read(sems.at[r, c])
            pltpu.semaphore_wait(sems.at[r, c], v)

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR((m, n)))

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    y = jax.block_until_ready(
        pl.pallas_call(
            kernel,
            out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.SMEM),
            out_shape=jax.ShapeDtypeStruct((m, n), jnp.int32),
        )()
    )
    np.testing.assert_array_equal(
        y, jnp.arange(m * n).astype(jnp.int32).reshape((m, n))
    )

  def test_hbm_hbm_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_cannot_dma_with_nonscalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))

    # TODO(b/345534352): Add interpret support for nonscalar semaphores.
    with self.assertRaisesRegex(ValueError, 'Cannot signal'):
      x = jnp.arange(8 * 128.).reshape((8, 128))
      pl.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

  def test_dma_with_scalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem.at[0]).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))
    x = jnp.arange(8 * 128.).reshape((8, 128))

    # TODO(b/345534352): Add interpret support for nonscalar semaphores.
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_hbm_hbm_grid_dma(self):
    # When using the grid, we have to emit Mosaic window_params. Test that they
    # work correctly with ANY memory space operands.
    def kernel(x_hbm_ref, y_hbm_ref):
      i = pl.program_id(0)
      def body(sem):
        pltpu.async_copy(
            x_hbm_ref.at[pl.ds(i, 1)], y_hbm_ref.at[pl.ds(i, 1)], sem
        ).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.float32),
        grid=(2,),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], x_ref.at[:, pl.ds(128)],
                         sem).wait()
        y_ref[...] = x_ref[...]
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_hbm_ref, y_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(x_ref, y_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pltpu.run_scoped(body,
                       pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY)],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_smem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[0, 0] * jnp.ones_like(y_ref)
      pltpu.run_scoped(body, pltpu.SMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = 4 * jnp.ones((8, 128), jnp.float32)
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_smem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[0, 0] = 0.0
        y_ref[0, 1] = x_ref[4, 4]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.SMEM((1, 2), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.SMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((1, 2), jnp.float32),
    )(x)
    expected = jnp.zeros_like(x[0:1, 0:2]).at[0, 1].set(x[4, 4])
    np.testing.assert_allclose(y, expected)

  def test_vmem_vmem_dma(self):
    def kernel(x_ref, y_ref):
      def body(sem):
        pltpu.async_copy(x_ref, y_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_slicing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(0, 8)], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(8, 8)], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((16, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_indexing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((16, 128)))

  def test_hbm_vmem_dma_multiple_indexing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        for i in range(3):
          dma1 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 0], y_ref.at[i].at[pl.ds(0, 8)],
              sem
          )
          dma2 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 1], y_ref.at[i].at[pl.ds(8, 8)],
              sem
          )
          dma1.wait()
          dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(3 * 2 * 8 * 128.).reshape((3, 2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((3, 16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((3, 16, 128)))

  def test_cannot_squeeze_lane_sublane(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    with self.assertRaises(Exception):
      _ = self.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
      )(x)

  @parameterized.named_parameters(
      ('', False),
      ('_interpret', True),
  )
  def test_hoisted_scratch_space(self, interpret):
    def kernel(x_ref, y_ref, scratch_ref):
      i = pl.program_id(0)
      @pl.when(i == 0)
      def _():
        scratch_ref[...] = x_ref[...]
      scratch_ref[...] += jnp.ones_like(scratch_ref)

      @pl.when(i == 2)
      def _():
        y_ref[...] = scratch_ref[...]

    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec((8, 128), lambda i: (0, 0)),
            ],
            scratch_shapes=[pltpu.VMEM((8, 128), jnp.float32)],
            out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
            grid=(3,),
        ),
        interpret=interpret,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x + 3)

  def test_hoisted_smem_space(self):
    # TODO(sharadmv,apaszke): enable SMEM scratch spaces
    # TODO(sharadmv,apaszke): add support for ()-shaped SMEM refs
    self.skipTest('Currently doesn\'t work')
    def kernel(y_ref, scratch_ref):
      scratch_ref[0, 0] = pl.program_id(0)
      y_ref[...] = jnp.broadcast_to(scratch_ref[0, 0], y_ref.shape)

    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[],
            scratch_shapes=[pltpu.SMEM((1, 1), jnp.int32)],
            out_specs=pl.BlockSpec((None, 8, 128), lambda i: (i, 0, 0)),
            grid=(2,),
        ),
        debug=True,
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.int32),
    )()
    expected = jnp.broadcast_to(jnp.arange(2, dtype=jnp.int32)[..., None, None],
                                (2, 8, 128))
    np.testing.assert_array_equal(y, expected)

  def test_hoisted_semaphore(self):
    def kernel(x_bbm_ref, y_ref, sem, dma_sem):
      pltpu.semaphore_signal(sem)
      pltpu.semaphore_wait(sem)
      pltpu.async_copy(x_bbm_ref, y_ref, dma_sem).wait()

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
            ],
            scratch_shapes=[pltpu.SemaphoreType.REGULAR,
                            pltpu.SemaphoreType.DMA],
            out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_large_array_indexing(self):
    n = 6
    dtype = jnp.bfloat16
    x = jax.lax.broadcasted_iota(dtype, (n, 1024 * 1024, 512), 0)

    def kernel(index, x, y, sem):
      pltpu.async_copy(x.at[index[0]], y.at[:], sem).wait()

    run = self.pallas_call(kernel,
                         grid_spec=pltpu.PrefetchScalarGridSpec(
                             num_scalar_prefetch=1,
                             in_specs=[
                                 pl.BlockSpec(
                                     memory_space=pltpu.TPUMemorySpace.ANY)],
                             out_specs=pl.BlockSpec(
                                 memory_space=pltpu.TPUMemorySpace.ANY),
                             scratch_shapes=[pltpu.SemaphoreType.DMA],
                             ),
                         out_shape=jax.ShapeDtypeStruct(x.shape[1:], dtype),
                         )

    for i in range(x.shape[0]):
      y = run(jnp.array([i], dtype=jnp.int32), x)
      np.testing.assert_array_equal(y, i)
      del y

  def test_local_dma(self):
    def test_kernel(x_ref,
                o_ref,
                copy_sem,
                ):
      o_ref[...] = jnp.zeros_like(o_ref[...])
      input_to_output_copy = pltpu.make_async_copy(
          src_ref=x_ref.at[0:8],
          dst_ref=o_ref.at[0:8],
          sem=copy_sem,
      )
      input_to_output_copy.start()
      input_to_output_copy.wait()

    out_shape = (jax.ShapeDtypeStruct((9, 128), jnp.float32))
    grid_spec = pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
            ],
            scratch_shapes=(
                [pltpu.SemaphoreType.DMA]
            )
        )

    kernel = self.pallas_call(
        test_kernel,
        out_shape=out_shape,
        grid_spec=grid_spec,
    )
    x = jax.random.normal(jax.random.key(0), shape=(16, 128))
    result = kernel(x)
    np.testing.assert_array_equal(result[0:8], x[0:8])
    np.testing.assert_array_equal(result[8:], jnp.zeros_like(result[8:]))

  @parameterized.parameters(('left',), ('right',))
  def test_remote_dma_ppermute(self, permutation):
    if jax.device_count() <= 1:
      self.skipTest('Test requires multiple devices.')
    num_devices = jax.device_count()
    if permutation == 'left':
      permute_fn = lambda x: lax.rem(x + num_devices - 1, num_devices)
    else:
      permute_fn = lambda x: lax.rem(x + num_devices + 1, num_devices)

    # Construct a kernel which performs a ppermute based on permute_fn.
    def test_kernel(x_ref,
                    o_ref,
                    copy_send_sem,
                    copy_recv_sem,
                ):
      o_ref[...] = jnp.zeros_like(o_ref[...])
      my_id = lax.axis_index('x')
      dst_device = permute_fn(my_id)
      input_to_output_copy = pltpu.make_async_remote_copy(
          src_ref=x_ref,
          dst_ref=o_ref,
          send_sem=copy_send_sem,
          recv_sem=copy_recv_sem,
          device_id=dst_device,
          device_id_type=pltpu.DeviceIdType.LOGICAL,
      )
      input_to_output_copy.start()
      input_to_output_copy.wait()

    out_shape = (jax.ShapeDtypeStruct((8, 128), jnp.float32))
    grid_spec = pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
            ],
            scratch_shapes=(
                [pltpu.SemaphoreType.DMA] * 2
            )
        )

    devices = mesh_utils.create_device_mesh((1, num_devices))
    mesh = jax.sharding.Mesh(devices, P(None, 'x'))
    sharding = jax.sharding.NamedSharding(mesh, P(None, 'x'))
    unsharded_arr = jax.random.normal(
        jax.random.key(0), shape=(8, 128 * num_devices))
    sharded_arr = jax.device_put(unsharded_arr, sharding)

    kernel = self.pallas_call(
        test_kernel,
        out_shape=out_shape,
        grid_spec=grid_spec,
    )
    compiled_func = jax.jit(shard_map.shard_map(
      kernel,
      mesh=mesh,
      in_specs=P(None, 'x'),
      out_specs=P(None, 'x'),
      check_rep=False))
    result = compiled_func(sharded_arr)

    perm = tuple((src, permute_fn(src)) for src in range(num_devices))
    perm = jax.tree_util.tree_map(int, perm)
    def lax_permute(x):
      return lax.ppermute(x, 'x', perm)
    expected = jax.jit(shard_map.shard_map(lax_permute,
                                   mesh=mesh,
                                   in_specs=P(None, 'x'),
                                   out_specs=P(None, 'x')))(sharded_arr)
    np.testing.assert_array_equal(result, expected)


class PallasCallRemoteDMATest(parameterized.TestCase):

  def setUp(self):
    if jax.device_count() < 2:
      self.skipTest('Only >=2 devices are supported.')
    if not jtu.is_device_tpu_at_least(5):
      self.skipTest('Only works with TPU v5')

    super().setUp()

  @parameterized.named_parameters(
      ('vmem', pltpu.TPUMemorySpace.VMEM),
      ('hbm', pltpu.TPUMemorySpace.ANY),
  )
  def test_basic_remote_vmem_dma(self, mem):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        dev_id = pltpu.device_id()
        other_dev_id = 1 - dev_id
        pltpu.semaphore_signal(ready_sem, device_id=other_dev_id,
                               device_id_type=pltpu.DeviceIdType.LOGICAL)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, other_dev_id,
            device_id_type=pltpu.DeviceIdType.LOGICAL,
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    x = jnp.arange(2 * 8 * 128.0).reshape((2 * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=mem)],
          out_specs=pl.BlockSpec(memory_space=mem),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

    devices = jax.devices()[:2]
    mesh = jax.sharding.Mesh(devices, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(
      ('left', 'left'),
      ('right', 'right')
  )
  def test_pallas_call_axis_index(self, direction):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, num_devices)
        else:
          neighbor = lax.rem(my_id - 1, num_devices)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + num_devices, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(('left', 'left'), ('right', 'right'))
  def test_pallas_call_axis_index_2d_mesh(self, direction):
    # Implements very simple collective permute in a 2D mesh.
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        my_other_id = lax.axis_index('y')
        axis_size = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, axis_size)
        else:
          neighbor = lax.rem(my_id - 1, axis_size)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + axis_size, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=(my_other_id, neighbor))
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=(my_other_id, neighbor)
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(
          body,
          pltpu.SemaphoreType.REGULAR,
          pltpu.SemaphoreType.DMA,
          pltpu.SemaphoreType.DMA,
      )

    axis_size = jax.device_count() // 2
    x = jnp.arange(axis_size * 8 * 128).reshape((axis_size * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (2, axis_size), jax.devices()
    )
    mesh = jax.sharding.Mesh(device_mesh, ['y', 'x'])
    y = jax.jit(
        shard_map.shard_map(
            body,
            mesh,
            in_specs=P('x', None),
            out_specs=P('x', None),
            check_rep=False,
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  def test_barrier_semaphore(self):
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        neighbor = lax.rem(my_id + 1, num_devices)
        barrier_sem = pltpu.get_barrier_semaphore()
        pltpu.semaphore_signal(barrier_sem, device_id=neighbor)
        pltpu.semaphore_wait(barrier_sem)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        ).wait()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
          compiler_params=dict(mosaic=dict(collective_id=0)),
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[-8:], x[:-8]])
    np.testing.assert_allclose(y, expected)


class PallasCallTest(PallasBaseTest):

  def test_cost_analysis(self):
    def kernel(x, y):
      y[:] = x[:]
    x = jnp.arange(1024.).reshape(8, 128)
    f = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        compiler_params=dict(
            mosaic=dict(
                cost_estimate=pltpu.CostEstimate(
                    flops=1234, transcendentals=21, bytes_accessed=12345
                )
            )
        ),
    )
    (analysis_result,) = jax.jit(f).lower(x).compile().cost_analysis()
    self.assertEqual(analysis_result['flops'], 1234)
    self.assertEqual(analysis_result['transcendentals'], 21)
    self.assertEqual(analysis_result['bytes accessed'], 12345)

  def test_vmem_limit(self):
    shape = (128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    x = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    with self.assertRaises(xla_extension.XlaRuntimeError):
      pl.pallas_call(
          kernel,
          out_shape=x,
          compiler_params=dict(mosaic=dict(vmem_limit_bytes=256)),
      )(x)
    pl.pallas_call(
        kernel,
        out_shape=x,
        compiler_params=dict(mosaic=dict(vmem_limit_bytes=int(2**18))),
    )(x)

  def test_allow_input_fusion(self):
    shape = (3, 128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    def f(x, y):
      z = jax.numpy.add(x, y)
      return pl.pallas_call(
          kernel,
          grid=(3,),
          in_specs=[pl.BlockSpec((1, 128, 128), lambda i: (i, 0, 0))],
          out_specs=pl.BlockSpec((1, 128, 128), lambda i: (i, 0, 0)),
          out_shape=x,
          compiler_params=dict(mosaic=dict(allow_input_fusion=[True])),
      )(z)

    x = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    y = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)

    out = f(x, y)
    expected = x + y
    np.testing.assert_array_equal(out, expected)
    compiled = jax.jit(f).lower(x, y).compile().as_text()
    assert re.search(r'fusion.*kind=kCustom.*fused_computation', compiled)

  def test_set_internal_scratch_size(self):
    shape = (128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    requested_bytes = 128 * 4
    with self.assertRaisesRegex(
        Exception,
        f'Requested internal scratch size {requested_bytes} needs to be at'
        ' least',
    ):
      pl.pallas_call(
          kernel,
          out_shape=jax.ShapeDtypeStruct(shape, jnp.float32),
          compiler_params=dict(
              mosaic=dict(internal_scratch_in_bytes=requested_bytes)
          ),
      )(x)


class PallasCallUnblockedIndexingTest(PallasBaseTest):

  def test_unblocked_indexing(self):
    shape = (16 * 8, 128)
    result_ty = jax.ShapeDtypeStruct((15 * 8, 128), jnp.float32)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[pl.ds(0, 8)] + x_ref[pl.ds(8, 8)]

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    y = self.pallas_call(
        kernel,
        grid=(15,),
        in_specs=(
            pl.BlockSpec(
                (2 * 8, 128), lambda i: (i * 8, 0), indexing_mode=pl.unblocked
            ),
        ),
        out_specs=pl.BlockSpec((8, 128), lambda i: (i, 0)),
        out_shape=result_ty,
    )(x)
    ref = []
    for i in range(15):
      ref.append(x[i * 8:(i + 1) * 8] + x[(i + 1) * 8:(i + 2) * 8])
    ref = np.concatenate(ref, axis=0)
    np.testing.assert_array_equal(y, ref)

  def test_unblocked_indexing_with_padding(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct((8, 128), jnp.float32)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[pl.ds(0, 8)]

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    y = self.pallas_call(
        kernel,
        grid=(1,),
        in_specs=(
            pl.BlockSpec(
                (2 * 8, 128),
                lambda i: (0, 0),
                indexing_mode=pl.Unblocked(((0, 8), (0, 0))),
            ),
        ),
        out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
        out_shape=result_ty,
    )(x)
    np.testing.assert_array_equal(y, x)


class PallasCallUnblockedIndexingInterpreterTest(
    PallasCallUnblockedIndexingTest
):
  INTERPRET = True


class PallasUXTest(PallasBaseTest):

  def test_mlir_location(self):
    # Make sure that MLIR locations are correctly propagated to primitives.
    args = (jax.ShapeDtypeStruct((8, 128), jnp.float32),)
    f = example_kernel.double
    as_tpu_kernel = mosaic.as_tpu_kernel
    def capture_as_tpu_kernel(module, *args, **kwargs):
      asm = module.operation.get_asm(enable_debug_info=True)
      self.assertIn('example_kernel.py":25', asm)
      return as_tpu_kernel(module, *args, **kwargs)
    mosaic.as_tpu_kernel = capture_as_tpu_kernel
    try:
      jax.jit(f).lower(*args)
    finally:
      mosaic.as_tpu_kernel = as_tpu_kernel


class PallasMegacoreTest(PallasBaseTest):

  def setUp(self):
    if jtu.device_under_test() != 'tpu':
      self.skipTest('Test only works on TPU')

    super().setUp()

  def test_megacore_splitting(self):
    # We want to make sure a 3-sized dimension is split across megacore
    # correctly, and if we combine the (3, 3) dimensions together it is still
    # correct.

    def matmul_kernel(x_ref, y_ref, z_ref):
      @pl.when(pl.program_id(2) == 0)
      def _():
        z_ref[...] = jnp.zeros_like(z_ref)
      z_ref[...] += x_ref[...] @ y_ref[...]

    k1, k2 = jax.random.split(jax.random.key(0))
    x = jax.random.uniform(k1, (3, 3, 512, 512))
    y = jax.random.uniform(k2, (3, 3, 512, 512))

    z = jax.vmap(
        jax.vmap(
            pl.pallas_call(
                matmul_kernel,
                out_shape=jax.ShapeDtypeStruct((512, 512), jnp.float32),
                grid=(4, 4, 4),
                in_specs=[
                    pl.BlockSpec((128, 128), lambda i, j, k: (i, k)),
                    pl.BlockSpec((128, 128), lambda i, j, k: (k, j)),
                ],
                out_specs=pl.BlockSpec((128, 128), lambda i, j, k: (i, j)),
                debug=True,
            )
        )
    )(x, y)
    np.testing.assert_allclose(z, jax.vmap(jax.vmap(jnp.dot))(x, y))


class PallasCallVmapTest(PallasBaseTest):

  def test_scratch_input_vmap(self):
    """Test that vmapp-ing a kernel with scratch inputs works correctly."""

    # Scratch inputs are only available for PallasTPU. This is why this test
    # does not live with the other vmap tests in:
    # jax/tests/pallas/pallas_test.py
    def add_one_with_scratch(x_ref, o_ref, scratch_ref):
      scratch_ref[...] = jnp.ones_like(scratch_ref[...])
      o_ref[...] = x_ref[...] + scratch_ref[...]

    tile_size = 128
    tile_shape = (tile_size, tile_size)
    array_shape = (2 * tile_size, 2 * tile_size)
    vmapped_add_one_with_scratch = jax.vmap(
        pl.pallas_call(
            add_one_with_scratch,
            out_shape=jax.ShapeDtypeStruct(array_shape, jnp.int32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                in_specs=[pl.BlockSpec(tile_shape, lambda i, j: (i, j))],
                out_specs=pl.BlockSpec(tile_shape, lambda i, j: (i, j)),
                scratch_shapes=[pltpu.VMEM(tile_shape, dtype=jnp.int32)],
                grid=(2, 2),
            ),
        )
    )

    x = jnp.broadcast_to(jnp.arange(array_shape[0]), (10, *array_shape))

    out = vmapped_add_one_with_scratch(x)
    out_ref = x + 1

    np.testing.assert_array_equal(out, out_ref, strict=True)


class PallasCallDynamicDMATest(PallasBaseTest):

  def setUp(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

    super().setUp()

  def test_simple_tile_aligned_dynamic_size_dma(self):

    def kernel(size_smem_ref, x_hbm_ref, _, o_hbm_ref, sem):
      size = size_smem_ref[0]
      pltpu.async_copy(
          x_hbm_ref.at[pl.ds(0, size)],
          o_hbm_ref.at[pl.ds(0, size)], sem).wait()

    x = jnp.tile(jnp.arange(8, dtype=jnp.int32)[:, None, None], [1, 8, 128])
    o = jnp.zeros((8, 8, 128), dtype=jnp.int32)
    size = jnp.array([4], dtype=jnp.int32)

    out = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
          num_scalar_prefetch=0,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM),
                    pl.BlockSpec(memory_space=pltpu.ANY),
                    pl.BlockSpec(memory_space=pltpu.ANY)],
          out_specs=pl.BlockSpec(memory_space=pltpu.ANY),
          scratch_shapes=[pltpu.SemaphoreType.DMA]
        ),
        out_shape=o,
        input_output_aliases={2: 0},
    )(size, x, o)
    expected = o.at[:4].set(x.at[:4].get())
    np.testing.assert_array_equal(out, expected)

  def test_simple_dynamic_size_dma(self):
    self.skipTest("doesn't work yet.")
    def kernel(size_smem_ref, x_hbm_ref, _, o_hbm_ref, sem):
      size = size_smem_ref[0]
      pltpu.async_copy(
          x_hbm_ref.at[pl.ds(0, size)],
          o_hbm_ref.at[pl.ds(0, size)], sem).wait()

    x = jnp.arange(8, dtype=jnp.int32)
    o = jnp.zeros(8, dtype=jnp.int32)
    size = jnp.array([4], dtype=jnp.int32)

    out = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
          num_scalar_prefetch=0,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM),
                    pl.BlockSpec(memory_space=pltpu.ANY),
                    pl.BlockSpec(memory_space=pltpu.ANY)],
          out_specs=pl.BlockSpec(memory_space=pltpu.ANY),
          scratch_shapes=[pltpu.SemaphoreType.DMA]
        ),
        out_shape=o,
        input_output_aliases={2: 0},
    )(size, x, o)
    expected = o.at[:4].set(x.at[:4].get())
    np.testing.assert_array_equal(out, expected)


class PallasCallPrintTest(PallasBaseTest):

  def test_debug_print(self):
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print('It works!')

    x = jnp.array([4.2, 2.4]).astype(jnp.float32)
    compiled_kernel = (
        jax.jit(kernel)
        .lower(x)
        .compile({'xla_tpu_enable_log_recorder': 'true'})
    )
    with jtu.capture_stderr() as get_output:
      jax.block_until_ready(compiled_kernel(x))
    self.assertIn('It works!', get_output())

  def test_debug_print_with_values(self):
    @functools.partial(
        self.pallas_call,
        in_specs=(pl.BlockSpec(memory_space=pltpu.SMEM),),
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print('x[0] == {}', x_ref[0])

    x = jnp.array([42, 24]).astype(jnp.int32)
    compiled_kernel = (
        jax.jit(kernel)
        .lower(x)
        .compile({'xla_tpu_enable_log_recorder': 'true'})
    )
    with jtu.capture_stderr() as get_output:
      jax.block_until_ready(compiled_kernel(x))
    self.assertIn('x[0] == 42', get_output())


class PallasCallTraceTest(PallasBaseTest):

  def parse_debug_string(self, debug_string):
    jaxpr, mlir = debug_string.split('module')
    return {'jaxpr': jaxpr, 'mlir': mlir}

  def test_trace_start_stop_match(self):
    def kernel(o_ref):
      with jax.named_scope('scope1'):
        o_ref[...] = jnp.zeros_like(o_ref[...])

    with string_stdout() as msg:
      _ = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        debug=True,
      )()
      # TODO(justinfu): Add an official lowering API to get the MLIR.
      mlir = self.parse_debug_string(msg.getvalue())['mlir']

    num_start = mlir.count('tpu.trace_start')
    num_stop = mlir.count('tpu.trace_stop')
    self.assertEqual(num_start, 1)
    self.assertEqual(num_stop, 1)

  def test_run_scoped(self):
    def kernel(o_ref):
      def scope1():
        with jax.named_scope('scope1'):
          o_ref[...] = jnp.zeros_like(o_ref[...])
      pltpu.run_scoped(scope1)

      def scope2():
        with jax.named_scope('scope2'):
          o_ref[...] = o_ref[...] + 1
      pltpu.run_scoped(scope2)

    with string_stdout() as msg:
      _ = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        debug=True,
      )()
      # TODO(justinfu): Add an official lowering API to get the MLIR.
      mlir = self.parse_debug_string(msg.getvalue())['mlir']

    num_start = mlir.count('tpu.trace_start')
    num_stop = mlir.count('tpu.trace_stop')
    self.assertEqual(num_start, 2)
    self.assertEqual(num_stop, 2)


class PallasCallCheckifyInterpreterTest(PallasBaseTest):
  INTERPRET: bool = True

  @parameterized.parameters((2,), (5,), (6,), (7,))
  def test_checkify_with_scalar_prefetch(self, threshold):
    def body(scalar_ref, x_ref, o_ref):
      scalar = scalar_ref[pl.program_id(0)]
      o_ref[...] = x_ref[...]
      checkify.check(scalar < threshold, 'failed on value {x}', x=scalar)

    s = jnp.array([4, 3, 2, 6, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    pallas_call = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[
                pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
            ],
            out_specs=pl.BlockSpec(
                (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
            ),
            grid=8,
        ),
    )
    checked_call = checkify.checkify(pallas_call)
    err, out = checked_call(s, x)
    expected_error_value = s[jnp.argmax(s >= threshold)]
    with self.assertRaisesRegex(
        checkify.JaxRuntimeError, f'failed on value {expected_error_value}'):
      err.throw()
    np.testing.assert_allclose(out, x.reshape((8, 8, -1))[s].reshape(x.shape))

  def test_checkify_with_scratch(self):
    def body(x_ref, o_ref, scratch_ref):
      scratch_ref[...] = x_ref[...]
      o_ref[...] = scratch_ref[...]
      all_nequal = ~jnp.all(o_ref[...] == x_ref[...])
      checkify.check(all_nequal, 'x_ref equals o_ref id=({x}, {y})',
                     x=pl.program_id(0), y=pl.program_id(1))

    x = jax.random.uniform(jax.random.key(0), (128, 128), dtype=jnp.float32)
    pallas_call = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.float32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec((32, 32), lambda i, j: (i, j)),
            ],
            out_specs=pl.BlockSpec((32, 32), lambda i, j: (i, j)),
            scratch_shapes=[pltpu.VMEM((32, 32), dtype=jnp.float32)],
            grid=(4, 4),
        ),
    )
    checked_call = checkify.checkify(pallas_call)
    err, out = checked_call(x)
    with self.assertRaisesRegex(
        checkify.JaxRuntimeError, r'x_ref equals o_ref id=\(0, 0\)'):
      err.throw()
    np.testing.assert_allclose(out, x)

  @parameterized.parameters((4,), (9,))
  def test_checkify_with_dynamic_grid(self, iteration):
    grid_size = 4
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1
      @pl.when(pl.program_id(0) == iteration)
      def _():
        checkify.check(False, f"error on iteration {iteration}")

    @jax.jit
    def dynamic_kernel(steps):
      pallas_call = self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )
      return checkify.checkify(pallas_call)()

    err, result = dynamic_kernel(jnp.int32(grid_size))
    if iteration < grid_size * 2:
      with self.assertRaisesRegex(
          checkify.JaxRuntimeError, f"error on iteration {iteration}"):
        err.throw()
    np.testing.assert_array_equal(
        result, np.full(shape, grid_size * 2.0, np.float32)
    )


class MiscellaneousInterpreterTest(PallasBaseTest):
  """Tests for recently reported bugs; only pass in interpret mode."""

  INTERPRET: bool = True

  def test_float32_stack(self):
    """b/347761105"""
    x = np.arange(128, dtype=jnp.float32).reshape(1, 128)
    y = x + 128

    def kernel(x_ref, y_ref, out_ref):
      out_ref[...] = jnp.stack([x_ref[...], y_ref[...]], axis=1)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 2, 128), jnp.float32)
    )(x, y)
    np.testing.assert_array_equal(out, np.stack([x, y], axis=1))

  def test_lane_to_chunk_reshape_bf16(self):
    """b/348038320"""
    x = np.arange(256 * 1024, dtype=jnp.bfloat16).reshape(1, 256, 1024)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.reshape(x_ref[...], (1, 256, 8, 128))

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 256, 8, 128), jnp.bfloat16)
    )(x)
    np.testing.assert_array_equal(out, np.reshape(x, (1, 256, 8, 128)))

  def test_lane_to_chunk_broadcast_fp32(self):
    """b/348033362"""
    x = np.arange(256 * 128, dtype=jnp.float32).reshape(1, 256, 128)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.broadcast_to(
          jnp.expand_dims(x_ref[...], 2), (1, 256, 8, 128)
      )

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 256, 8, 128), jnp.float32)
    )(x)
    np.testing.assert_array_equal(
        out, np.broadcast_to(np.expand_dims(x, 2), (1, 256, 8, 128))
    )

  def test_lane_dynamic_slice(self):
    """b/346849973"""
    x = np.arange(128, dtype=jnp.float32)

    def kernel(x_ref, out_ref):
      out_ref[...] = lax.dynamic_slice_in_dim(x_ref[...], 64, 1, 0)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1,), jnp.float32)
    )(x)
    np.testing.assert_array_equal(out, x[64:65])

  def test_lane_broadcast_bf16(self):
    """b/346654106"""
    x = np.arange(256, dtype=jnp.bfloat16).reshape(256, 1)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.broadcast_to(x_ref[...], (256, 512))

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((256, 512), jnp.bfloat16)
    )(x)
    np.testing.assert_array_equal(out, np.broadcast_to(x, (256, 512)))

  def test_bfloat16_to_uint32_bitcast(self):
    """b/347771903"""
    x = np.arange(16 * 2 * 256, dtype=jnp.bfloat16).reshape(16, 2, 256)

    def kernel(x_ref, out_ref):
      out_ref[...] = pltpu.bitcast(x_ref[...], jnp.uint32)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((16, 1, 256), jnp.uint32)
    )(x)
    # FIXME: Add correctness test for result.

  def test_roll_partial(self):
    """b/337384645"""
    x = np.arange(8192, dtype=jnp.float32).reshape(128, 64)

    def kernel(x_ref, out_ref):
      out_ref[...] = pltpu.roll(x_ref[...], 3, 1)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((128, 64), jnp.float32)
    )(x)
    np.testing.assert_array_equal(out, np.roll(x, 3, 1))


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
