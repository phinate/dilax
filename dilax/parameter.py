from __future__ import annotations

import abc

import equinox as eqx

import jax
import jax.numpy as jnp

from dilax.util import as1darray
from dilax.pdf import Flat, Gauss, Poisson, HashablePDF


class Parameter(eqx.Module):
    value: jax.Array = eqx.field(converter=as1darray)
    bounds: tuple[jnp.array, jnp.array] = eqx.field(
        static=True, converter=lambda x: tuple(map(as1darray, x))
    )
    constraints: set[HashablePDF] = eqx.field(static=True)

    def __init__(
        self,
        value: jax.Array,
        bounds: tuple[jnp.array, jnp.array],
    ) -> None:
        self.value = value
        self.bounds = bounds
        self.constraints: set[HashablePDF] = set()

    def update(self, value: jax.Array) -> Parameter:
        return self.__class__(value=value, bounds=self.bounds)

    @property
    def boundary_penalty(self) -> jax.Array:
        return jnp.where(
            (self.value < self.bounds[0]) | (self.value > self.bounds[1]),
            jnp.inf,
            0,
        )


class Effect(eqx.Module):
    @property
    @abc.abstractmethod
    def constraint(self) -> HashablePDF:
        ...

    @abc.abstractmethod
    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        ...

    def __call__(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        return jnp.atleast_1d(self.scale_factor(parameter=parameter, sumw=sumw)) * sumw


class unconstrained(Effect):
    @property
    def constraint(self) -> HashablePDF:
        return Flat()

    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        return parameter.value


class gauss(Effect):
    width: jax.Array = eqx.field(static=True, converter=as1darray)

    def __init__(self, width: jax.Array) -> None:
        self.width = width

    @property
    def constraint(self) -> HashablePDF:
        return Gauss(mean=0.0, width=1.0)

    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        return parameter.value * self.width + 1


class shape(Effect):
    up: jax.Array = eqx.field(converter=as1darray)
    down: jax.Array = eqx.field(converter=as1darray)

    def __init__(
        self,
        up: jax.Array,
        down: jax.Array,
    ) -> None:
        self.up = up  # +1 sigma
        self.down = down  # -1 sigma

    @eqx.filter_jit
    def vshift(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        factor = parameter.value
        dx_sum = self.up + self.down - 2 * sumw
        dx_diff = self.up - self.down

        # taken from https://github.com/nsmith-/jaxfit/blob/8479cd73e733ba35462287753fab44c0c560037b/src/jaxfit/roofit/combine.py#L173C6-L192
        _asym_poly = jnp.array([3.0, -10.0, 15.0, 0.0]) / 8.0

        abs_value = jnp.abs(factor)
        morph = 0.5 * (
            dx_diff * factor
            + dx_sum
            * jnp.where(
                abs_value > 1.0,
                abs_value,
                jnp.polyval(_asym_poly, factor * factor),
            )
        )

        return morph

    @property
    def constraint(self) -> HashablePDF:
        return Gauss(mean=0.0, width=1.0)

    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        return jax.numpy.clip(
            (sumw + self.vshift(parameter=parameter, sumw=sumw)) / sumw,
            a_min=1e-5,
        )


class lnN(Effect):
    width: jax.Array | tuple[jax.Array, jax.Array] = eqx.field(static=True)

    def __init__(
        self,
        width: jax.Array | tuple[jax.Array, jax.Array],
    ) -> None:
        self.width = width

    def scale(self, parameter: Parameter) -> jax.Array:
        if isinstance(self.width, tuple):
            down, up = self.width
            scale = jnp.where(parameter.value > 0, up, down)
        else:
            scale = self.width
        return scale

    @property
    def constraint(self) -> HashablePDF:
        return Gauss(mean=0.0, width=1.0)

    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        width = self.scale(parameter=parameter)
        return jnp.exp(parameter.value * width)


class poisson(Effect):
    lamb: jax.Array = eqx.field(static=True, converter=as1darray)

    def __init__(self, lamb: jax.Array) -> None:
        self.lamb = lamb

    @property
    def constraint(self) -> HashablePDF:
        return Gauss(mean=0.0, width=1.0)

    def scale_factor(self, parameter: Parameter, sumw: jax.Array) -> jax.Array:
        gauss_cdf = jnp.broadcast_to(self.constraint.cdf(parameter.value), self.lamb.shape)
        val = Poisson(self.lamb).inv_cdf(gauss_cdf)
        return val


class ModifierBase(eqx.Module):
    @abc.abstractmethod
    def scale_factor(self, sumw: jax.Array) -> jax.Array:
        ...


class modifier(ModifierBase):
    """
    Create a new modifier for a given parameter and penalty.

    Example:
    ```
        from dilax.parameter import modifier, Parameter, unconstrained

        mu = Parameter(value=1.1, bounds=(0, 100))

        # create a new parameter and a penalty
        modify = modifier(name="mu", parameter=mu, effect=unconstrained())

        # apply the modifier
        modify(jnp.array([10, 20, 30]))
        >> ('mu',
            Array([11., 22., 33.], dtype=float32, weak_type=True),
            Array([0.], dtype=float32))

        # lnN effect
        norm = Parameter(value=0.0, bounds=(-jnp.inf, jnp.inf))
        modify = modifier(name="norm", parameter=norm, effect=lnN(0.2))
        modify(jnp.array([10, 20, 30]))

        # poisson effect
        hist = jnp.array([10, 20, 30])
        modify = modifier(name="norm", parameter=norm, effect=poisson(hist))
        modify(jnp.array([10, 20, 30]))

        # shape effect
        up = jnp.array([12, 23, 35])
        down = jnp.array([8, 19, 26])
        modify = modifier(name="norm", parameter=norm, effect=shape(up, down))
        modify(jnp.array([10, 20, 30]))
    ```
    """

    name: str
    parameter: Parameter
    effect: Effect

    def __init__(self, name: str, parameter: Parameter, effect: Effect = unconstrained()) -> None:
        self.name = name
        self.parameter = parameter
        self.effect = effect
        self.parameter.constraints.add(self.effect.constraint)

    def scale_factor(self, sumw: jax.Array) -> jax.Array:
        return self.effect.scale_factor(parameter=self.parameter, sumw=sumw)

    def __call__(self, sumw: jax.Array) -> tuple[jax.Array, jax.Array]:
        return jnp.atleast_1d(self.scale_factor(sumw=sumw)) * sumw


class compose(ModifierBase):
    """
    Composition of multiple modifiers, i.e.: `(f ∘ g ∘ h)(hist) = f(hist) * g(hist) * h(hist)`
    It behaves like a single modifier, but it is composed of multiple modifiers; it can be arbitrarly nested.

    Example:
    ```
        from dilax.parameter import modifier, compose, Parameter, FreeFloating, LogNormal

        mu = Parameter(value=1.1, bounds=(0, 100))
        sigma = Parameter(value=0.1, bounds=(-100, 100))

        # create a new parameter and a composition of modifiers
        composition = compose(
            modifier(name="mu", parameter=mu),
            modifier(name="sigma1", parameter=sigma, effect=lnN(0.1)),
        )

        # apply the composition
        composition(jnp.array([10, 20, 30]))

        # nest compositions
        composition = compose(
            composition,
            modifier(name="sigma2", parameter=sigma, effect=lnN(0.2)),
        )

        # jit
        eqx.filter_jit(composition)(jnp.array([10, 20, 30]))
    ```
    """

    modifiers: tuple[modifier]
    names: list[str] = eqx.field(static=True)

    def __init__(self, *modifiers: tuple[modifier]) -> None:
        self.modifiers = modifiers

        # check for duplicate names
        duplicates = [name for name in self.names if self.names.count(name) > 1]
        if duplicates:
            raise ValueError(f"Modifier need to have unique names, got: {duplicates}")

    @property
    def names(self) -> list[str]:
        names = []
        for m in range(self.n_modifiers):
            modifier = self.modifiers[m]
            if isinstance(modifier, compose):
                names.extend(modifier.names)
            else:
                names.append(modifier.name)
        return list(names)

    @property
    def n_modifiers(self) -> int:
        return len(self.modifiers)

    def scale_factors(self, sumw: jax.Array) -> jax.Array:
        sfs = {}
        for m in range(self.n_modifiers):
            modifier = self.modifiers[m]
            if isinstance(modifier, compose):
                sfs.update(modifier.scale_factors(sumw=sumw))
            else:
                sf = jnp.atleast_1d(modifier.scale_factor(sumw=sumw))
                sfs[modifier.name] = jnp.broadcast_to(sf, sumw.shape)
        return sfs

    def scale_factor(self, sumw: jax.Array) -> jax.Array:
        return jnp.atleast_1d(
            jnp.prod(jnp.stack(list(self.scale_factors(sumw=sumw).values())), axis=0)
        )

    def __call__(self, sumw: jax.Array) -> tuple[jax.Array, jax.Array]:
        return jnp.atleast_1d(self.scale_factor(sumw=sumw)) * sumw
