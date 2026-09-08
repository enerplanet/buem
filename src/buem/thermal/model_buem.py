from __future__ import annotations

import logging
import os
import shutil
from typing import Any, ClassVar

import cvxpy as cp
import numpy as np
import pandas as pd
import pvlib
from dotenv import load_dotenv
from scipy.sparse import lil_matrix, vstack

from buem.config.validator import validate_cfg
from buem.thermal import dhw_cooking

logger = logging.getLogger(__name__)


class ModelBUEM:
    """
    ISO 13790 simplified hourly 5R1C building energy model.

    Computes annual heating and cooling demand at hourly resolution using a
    single-pass dead-band formulation solved as a Linear Programme (LP).

    Key aspects
    -----------
    - **Physics**: 5R1C thermal network (air / surface / mass nodes) with
      ISO 13790 §C.2 gain distribution (Schütz et al. 2017 Eqs. 20-22).
    - **Inputs**: Weather (TMY), structured building components (walls, roof,
      floor, windows, doors), occupancy profiles, internal/electrical gains.
    - **Solar gains**: Plane-of-array irradiance via pvlib (isotropic sky model);
      window transmittance and opaque effective collecting area per ISO 13790 §11.3.2.
    - **Solver**: L1-norm objective (min Σ|Q_HC|) → LP solved by CLARABEL
      (interior-point, OSQP fallback). Produces sparse Q_HC with exact dead-band
      zeros when indoor temperature stays within comfort bounds passively.
    - **MILP path** (experimental): Binary-variable formulation separating
      Q_heat / Q_cool via CBC / GLPK / PuLP.
    - **Periodicity**: Wrap-around mass-node dynamics (T_m(n-1) → T_m(0))
      enforce annual-periodic thermal mass temperature without an arbitrary
      initial condition.
    """
    CONST: ClassVar[dict[str, float]] = {
        # specific heat transfer coefficient between internal air and surface [kW/m²K]
        # ISO 13790 §7.2.2.2, h_is = 3.45 W/m²K
        "h_is": 3.45 / 1000,
        # non-dimensional ratio: total internal surface area / effective floor area
        # ISO 13790 §7.2.2.2, λ_at = 4.5
        "lambda_at": 4.5,
        # specific heat transfer coefficient thermal mass–surface [kW/m²K]
        # ISO 13790 §12.2.2, h_ms = 9.1 W/m²K
        "h_ms": 9.1 / 1000,
        # Exterior surface thermal resistance [m²K/kW], ISO 6946 Table 1.
        # R_se = 0.04 m²K/W = 40 m²K/kW (1 W = 0.001 kW  →  R [m²K/kW] = R [m²K/W] / 0.001 = R × 1000).
        # Used with h_r [kW/m²K] to compute window sky correction:
        #   thermal_rad_win [kW] = H_win [kW/K] × R_se [m²K/kW] × h_r [kW/m²K] × ΔT_sky [K]
        "R_se": 40.0,  # m²K/kW  (R_se = 0.04 m²K/W converted to kW-consistent units)
        # ASHRAE 140 : 2011, Table 5.3, page 18 (infrared emittance) (unused --> look at h_r)
        "epsilon": 0.9,
        # external specific radiative heat transfer [kW/m^2/K] (ISO 13790, Schuetz et al. 2017, 2.3.4)
        "h_r": 0.9 * 5.0 / 1000.0,
        # ASHRAE 140 : 2011, Table 5.3, page 18 (absorption opaque comps) - Netherlands typical light surfaces
        "alpha": 0.35,  # Realistic for Netherlands light-colored building surfaces
        # average difference external air temperature and sky temperature
        "delta_T_sky": 11.0,  # K
        # density air
        "rho_air": 1.2,  # kg/m^3
        # heat capacity air
        "C_air": 1.006,  # kJ/kg/K
        }

    def __init__(self, cfg: dict, maxLoad: float | None = None):
        """
        Initialise the model and declare cross-method attributes.

        Parameters
        ----------
        cfg : dict
            Building configuration.  Required top-level keys include
            ``'weather'`` (DataFrame with DNI/DHI/GHI/T), ``'components'``
            (structured wall/roof/floor/window/door definitions with U-values
            and element areas), ``'A_ref'``, ``'h_room'``, ``'thermalClass'``,
            ``'c_m'``, ``'comfortT_lb'``, ``'comfortT_ub'`` (each a scalar or a
            ``pd.Series`` aligned with ``'weather'``'s index, for schedule-driven
            setpoints), ``'Q_ig'``, ``'elecLoad'``, ``'occ_nothome'``, ``'occ_sleeping'``,
            ``'n_air_infiltration'``, ``'n_air_use'``, solar/shading factors,
            and geographic coordinates (``'latitude'``, ``'longitude'``).
        maxLoad : float, optional
            Override for maximum heating system capacity [kW].
            If *None*, computed from :meth:`calcDesignHeatLoad`.
        """
        self.cfg = cfg
        self.maxLoad = maxLoad

        # time series index
        self.times = self.cfg["weather"].index

        # irradiance per surface element (DataFrame indexed by time, cols = element ids)
        self._irrad_surf = pd.DataFrame(index=self.times)

        # component tree and per-component parameters
        # component_elements: dict[component] -> list[element dicts {id, area, azimuth, tilt, ...}]
        self.component_elements: dict[str, list[dict]] = {}
        # component-level U (same for all elements)
        self.bU: dict[str, Any] = {}
        # component conductance [kW/K] aggregated over elements (Original state)
        self.bH: dict[str, dict[str, float]] = {}
        # window element list (shortcut)
        self.windows: list[dict] = []

        # 5R1C thermal parameters (initialized later in _init5R1C)
        self.bA_f = None
        self.bA_m = None
        self.bH_ms = None
        self.bC_m = None
        self.bA_tot = None
        self.bH_is = None
        self.bT_comf_lb = None
        self.bT_comf_ub = None

        # profiles (internal gains, occupancy, solar gains created in _init5R1C)
        self.profiles: dict[str, Any] = {}
        self.profilesEval: dict[str, Any] = {}

        # results containers
        self.static_results: dict[str, Any] = {}
        self.detailedResults = pd.DataFrame(index=self.times)

        # solver/runtime bookkeeping
        self.components = ["Walls", "Roof", "Floor", "Windows", "Ventilation"]
        self.hasTypPeriods = False
        self.ventControl = bool(self.cfg.get("ventControl", False))

    # -------- utilities --------
    def _cfg_float(self, key, required=True):
        """Return ``cfg[key]`` as a float.  Raises ``ValueError`` if *required*
        and the key is missing or unconvertible."""
        if key not in self.cfg:
            if required:
                raise ValueError(f"Required configuration key '{key}' missing from cfg")
            else:
                raise KeyError(f"Configuration key '{key}' not found")

        v = self.cfg[key]
        try:
            return float(v)
        except (TypeError, ValueError) as e:
            # allow Series/array -> take mean as fallback ONLY if explicitly a Series
            if hasattr(v, 'mean'):
                try:
                    return float(v.mean())
                except (TypeError, ValueError):
                    raise ValueError(f"Cannot convert cfg['{key}'] to float: {v}, error: {e}")
            else:
                raise ValueError(f"Cannot convert cfg['{key}'] to float: {v}, error: {e}")

    # -------- parameter parsing --------
    def _initPara(self):
        """Ensure ``self.profiles`` and ``self.profilesEval`` dicts exist."""
        if not hasattr(self, "profiles"):
            self.profiles = {}
        if not hasattr(self, "profilesEval"):
            self.profilesEval = {}

    def _initEnvelop(self):
        """
        Parse ``cfg['components']`` and compute per-component conductances.

        Populates
        ---------
        self.component_elements : dict[str, list[dict]]
            Element dicts (id, area, azimuth, tilt, …) keyed by component name.
        self.bU : dict[str, float | None]
            Component-level U-value [W/m²K], or *None* when per-element U was used.
        self.bH : dict[str, dict[str, float]]
            Aggregated conductance ``bH[comp]['Original']`` [kW/K].
            Ventilation conductance is derived from ``n_air_infiltration``,
            ``n_air_use``, ``A_ref``, ``h_room``, and air properties.

        Falls back to legacy ``A_<Comp>`` / ``U_<Comp>`` keys when the
        structured ``components`` dict is absent.

        Raises
        ------
        ValueError
            If required components, U-values, or areas are missing or invalid.
        """
        comps = self.cfg.get("components")

        # If components missing or not a dict -> attempt legacy fallback (A_<Comp> keys)
        if not isinstance(comps, dict) or not comps:
            constructed = {}
            had_any = False
            for comp in ("Walls", "Roof", "Floor", "Windows", "Doors"):
                elems = []
                i = 1
                while True:
                    keyn = f"A_{comp}_{i}"
                    if keyn in self.cfg:
                        had_any = True
                        elems.append({"id": f"{comp}_{i}", "area": float(self.cfg[keyn])})
                        i += 1
                    else:
                        break
                if not elems and f"A_{comp}" in self.cfg:
                    had_any = True
                    area_val = float(self.cfg[f"A_{comp}"])
                    elems.append({"id": f"{comp}_1", "area": area_val})
                if elems:
                    # keep U as-is (may be None) and let the same processing logic handle it
                    constructed[comp] = {"U": self.cfg.get(f"U_{comp}"), "elements": elems}

            if had_any:
                # adopt constructed components for backward compatibility
                comps = constructed
                self.cfg["components"] = constructed
            else:
                # No structured components and no legacy area keys -> fail early
                raise ValueError("Configuration missing 'components' tree and no legacy A_<Comp> keys found.")

        # Now 'comps' is a dict (either originally provided or constructed)
        self.component_elements = {}
        self.bU = {}
        self.bH = {}

        for comp_name, comp_data in comps.items():
            if not isinstance(comp_data, dict):
                raise TypeError(f"components.{comp_name} must be an object")

            # Ventilation is not a physical surface with U-values per element;
            # its aggregated conductance is computed from infiltration rates below.
            # Skip area/U validation for ventilation components.
            if comp_name.lower() == "ventilation":
                self.component_elements[comp_name] = []  # no surface elements
                self.bU[comp_name] = None
                # ensure a placeholder so other code won't KeyError; H_ve is set later
                self.bH.setdefault(comp_name, {})
                continue

            elems = comp_data.get("elements", [])
            if not isinstance(elems, list):
                raise TypeError(f"components.{comp_name}.elements must be a list")
            parsed = []
            for e in elems:
                if "area" not in e:
                    raise ValueError(
                        f"components.{comp_name}: element {e.get('id', 'unknown')} is missing required 'area' field."
                    )
                parsed.append({
                    "id": e.get("id"),
                    "area": float(e["area"]),
                    "azimuth": float(e["azimuth"]) if e.get("azimuth") is not None else None,
                    "tilt": float(e["tilt"]) if e.get("tilt") is not None else None,
                    **{k: v for k, v in e.items() if k not in ("id", "area", "azimuth", "tilt")}
                })
            self.component_elements[comp_name] = parsed

            # Aggregated conductance: prefer component-level U, otherwise require per-element U
            b_trans = float(comp_data.get("b_transmission")) if "b_transmission" in comp_data else 1.0
            total_area = sum(e["area"] for e in parsed)

            u_val = comp_data.get("U")
            if u_val is None:
                # No component-level U provided -> require per-element U for all elements
                if parsed and all(e.get("U") is not None for e in parsed):
                    total_conductance = 0.0
                    for e in parsed:
                        if "U" not in e or "area" not in e:
                            raise ValueError(f"Element {e.get('id', 'unknown')} in {comp_name} missing U or area")
                        try:
                            e_b = float(e.get("b_transmission", 1.0))
                            total_conductance += float(e["U"]) * float(e["area"]) * e_b
                        except (TypeError, ValueError):
                            eid = e.get('id', 'unknown')
                            raise ValueError(
                                f"components.{comp_name}.elements contains "
                                f"invalid U or area for element {eid}"
                            )
                    # store None to indicate per-element U was used; bH uses computed conductance (kW/K)
                    self.bU[comp_name] = None
                    self.bH[comp_name] = {"Original": total_conductance / 1000.0}
                else:
                    # neither component-level U nor all elements have per-element U -> fail early
                    raise ValueError(
                        f"components.{comp_name} missing component U and not all elements provide per-element U. "
                        "Provide 'U' at the component level or 'U' for every element."
                    )
            else:
                try:
                    self.bU[comp_name] = float(u_val)
                except (TypeError, ValueError):
                    raise ValueError(f"components.{comp_name}.U invalid: {u_val}")
                self.bH[comp_name] = {"Original": self.bU[comp_name] * total_area * b_trans / 1000.0}

        # build helper lists and windows element list
        self.walls = [e["id"] for e in self.component_elements.get("Walls", [])]
        self.roofs = [e["id"] for e in self.component_elements.get("Roof", [])]
        self.floors = [e["id"] for e in self.component_elements.get("Floor", [])]
        self.windows = self.component_elements.get("Windows", [])

        if logger.isEnabledFor(logging.DEBUG):
            for comp_name, elements in self.component_elements.items():
                if not elements:
                    continue
                total_area = sum(float(e["area"]) for e in elements if "area" in e and e["area"] is not None)
                logger.debug("%s: %d elements, total area %.1f m2", comp_name, len(elements), total_area)
                for e in elements[:3]:
                    area_val = float(e["area"]) if "area" in e and e["area"] is not None else 0
                    logger.debug(
                        "  - %s: %.1f m2, az %s, tilt %s",
                        e.get("id", "unknown"), area_val,
                        e.get("azimuth", "default"), e.get("tilt", "default"),
                    )
                if len(elements) > 3:
                    logger.debug("  ... and %d more", len(elements) - 3)

        # ventilation aggregated conductance (kW/K) - NO DEFAULTS, strict validation
        if "A_ref" not in self.cfg:
            raise ValueError("A_ref (reference floor area) missing from configuration")
        A_ref = self._cfg_float("A_ref", required=True)
        if A_ref <= 0:
            raise ValueError(f"A_ref must be > 0, got: {A_ref}")

        if "h_room" not in self.cfg:
            raise ValueError("h_room (room height) missing from configuration")
        h_room = self._cfg_float("h_room", required=True)
        # Upper bound covers non-residential spaces (warehouses, sports halls,
        # industrial halls) as well as residential room heights, not just the latter.
        if h_room <= 0 or h_room > 20.0:
            raise ValueError(f"h_room ({h_room}) must be between 0 and 20.0 meters")

        rho_air = self.CONST["rho_air"]
        C_air = self.CONST["C_air"]

        if "n_air_infiltration" not in self.cfg:
            raise ValueError("n_air_infiltration missing from configuration")
        if "n_air_use" not in self.cfg:
            raise ValueError("n_air_use missing from configuration")

        n_air_inf = self._cfg_float("n_air_infiltration", required=True)
        n_air_use = self._cfg_float("n_air_use", required=True)

        if n_air_inf < 0 or n_air_use < 0:
            raise ValueError(f"Air change rates cannot be negative: inf={n_air_inf}, use={n_air_use}")
        if (n_air_inf + n_air_use) > 10.0:
            logger.warning("Very high air change rate: %.2f /h", n_air_inf + n_air_use)

        H_ve = A_ref * h_room * rho_air * C_air * (n_air_inf + n_air_use) / 3600.0
        self.bH.setdefault("Ventilation", {})["Original"] = H_ve

    def _resolve_opaque_element_u(self, comp_name: str, e: dict) -> float:
        """U-value [W/m²K] to use for one opaque element's own solar-gain
        term (Walls/Doors/Roof; Windows use ``g_gl``, not U, so this
        doesn't apply there).

        Mirrors the same two-mode handling ``_initEnvelop`` already uses
        for the H/conductance calc: prefer the component-level U
        (``self.bU[comp_name]``) when one was provided; when it's
        ``None`` (``_initEnvelop``'s documented sentinel for "per-element
        U was used instead" -- e.g. a component mixing exposed and party
        walls, where no single value could represent both), fall back to
        this element's own ``U``. Without this fallback, a component-
        level-U-less component crashed here with ``TypeError`` (``None``
        used as a numeric multiplier) even though ``_initEnvelop`` had
        already validated and used each element's own U correctly a few
        lines earlier for H -- see CHANGELOG.md.
        """
        comp_u = self.bU.get(comp_name)
        if comp_u is not None:
            return comp_u
        eid = e.get("id", "unknown")
        if e.get("U") is None:
            raise ValueError(
                f"components.{comp_name}: element {eid} has no per-element "
                "U and no component-level U is set -- cannot compute solar "
                "gain for this element."
            )
        return float(e["U"])

    # -------- 5R1C & solar --------
    def _init5R1C(self):
        """
        Compute ISO 13790 5R1C thermal network parameters and build solar gain
        profiles.

        Derived quantities stored on *self*:

        - ``bA_f``  – heated floor area [m²] (= A_ref)
        - ``bA_m``  – effective mass area [m²] (= A_ref × f_a(thermalClass))
        - ``bH_ms`` – mass–surface conductance [kW/K] (= A_m × h_ms)
        - ``bC_m``  – internal heat capacity [kWh/K] (= A_ref × c_m / 3600)
        - ``bA_tot``– total internal surface area [m²] (= A_ref × λ_at)
        - ``bH_is`` – surface–air conductance [kW/K] (= A_tot × h_is)
        - ``bT_comf_lb / bT_comf_ub`` – dead-band comfort bounds [°C], length-n
          arrays (broadcast from a scalar, or taken directly from a per-timestep
          ``pd.Series`` in ``cfg``)

        Solar gain profiles stored in ``self.profiles``:

        - ``bQ_sol_Windows``  – window solar gains [kW] (incl. sky radiation correction)
        - ``bQ_sol_Walls``    – wall + door opaque solar gains [kW]
        - ``bQ_sol_Roof``     – roof opaque solar gains [kW]
        - ``bQ_sol_Floor``    – zero by design (floor faces down)
        - ``bQ_sol_Opaque``   – walls + roof + floor combined [kW]

        Window gains use ``g_gl × (1−F_f) × F_w × POA``.
        Opaque gains use ISO 13790 §11.3.2: ``A_sol = α × R_se × U × A``.
        POA irradiance is computed via pvlib (isotropic sky model) in
        :meth:`_calcRadiation`.
        """
        # store constants reference
        self.bConst = self.CONST

        # thermal capacity class lookup (ISO 13790 table): only f_a factor needed now
        # c_m is explicitly provided in cfg (default 175 kJ/m²K = medium class midpoint)
        bClass_f_a = {"very light": 2.5, "light": 2.5, "medium": 2.5, "heavy": 3.0, "very heavy": 3.5}

        # Heated floor area and basic derived thermal params - NO DEFAULTS
        if "A_ref" not in self.cfg:
            raise ValueError("A_ref (reference floor area) required in configuration")
        A_ref = self.cfg["A_ref"]
        if A_ref is None or float(A_ref) <= 0:
            raise ValueError(f"A_ref (reference floor area) must be > 0, got: {A_ref}")
        self.bA_f = float(A_ref)

        thermalClass = self.cfg.get("thermalClass")
        if thermalClass is None:
            raise ValueError("thermalClass must be specified (very light, light, medium, heavy, very heavy)")
        if thermalClass not in bClass_f_a:
            raise ValueError(f"Invalid thermalClass '{thermalClass}'. Must be one of: {list(bClass_f_a.keys())}")

        self.bA_m = self.bA_f * bClass_f_a[thermalClass]
        self.bH_ms = self.bA_m * self.bConst["h_ms"]

        # specific heat c_m [kJ/m²K] → internal heat capacity [kWh/K]
        if "c_m" not in self.cfg:
            raise ValueError("c_m (specific thermal capacity, kJ/m²K) must be present in cfg")
        self.bC_m = self.bA_f * float(self.cfg["c_m"]) / 3600.0

        # internal surface area and surface-air conductance
        self.bA_tot = self.bA_f * self.bConst["lambda_at"]
        self.bH_is = self.bA_tot * self.bConst["h_is"]

        # comfort bounds - must be provided, no defaults.
        # Accepted as either a scalar (applied to every timestep, as before) or a
        # pd.Series aligned with cfg['weather'].index -- lets a building express a
        # real occupied/unoccupied setpoint schedule (e.g. a school closed nights,
        # weekends, and summer) instead of only the annual F_red_htr scalar, which
        # was designed for residential night-setback. Normalized to a length-n
        # array immediately so every downstream consumer (LP/MILP constraints,
        # big-M bounds, T_set) needs no further scalar/array branching.
        if "comfortT_lb" not in self.cfg:
            raise ValueError("comfortT_lb (lower comfort temperature) must be specified")
        if "comfortT_ub" not in self.cfg:
            raise ValueError("comfortT_ub (upper comfort temperature) must be specified")

        comfortT_lb_raw = self.cfg["comfortT_lb"]
        comfortT_ub_raw = self.cfg["comfortT_ub"]

        if comfortT_lb_raw is None or comfortT_ub_raw is None:
            raise ValueError("comfortT_lb and comfortT_ub must be specified for thermal simulation")

        n_steps = len(self.times)

        def _to_comfort_array(value: Any, name: str) -> np.ndarray:
            if isinstance(value, pd.Series):
                if len(value) != n_steps:
                    raise ValueError(
                        f"{name} series length ({len(value)}) must match the weather "
                        f"timeseries length ({n_steps})"
                    )
                return value.to_numpy(dtype=float)
            return np.full(n_steps, float(value))

        comfortT_lb = _to_comfort_array(comfortT_lb_raw, "comfortT_lb")
        comfortT_ub = _to_comfort_array(comfortT_ub_raw, "comfortT_ub")

        if np.any(comfortT_lb >= comfortT_ub):
            raise ValueError("comfortT_lb must be < comfortT_ub at every timestep")
        # Sanity range covers non-residential setpoints (frost-protection-only
        # unheated/lightly-conditioned industrial space) as well as residential
        # comfort, not just the latter.
        if np.any(comfortT_lb < 5) or np.any(comfortT_ub > 35):
            raise ValueError(
                "Comfort temperatures unreasonable: "
                f"lb range=[{comfortT_lb.min()}, {comfortT_lb.max()}], "
                f"ub range=[{comfortT_ub.min()}, {comfortT_ub.max()}]"
            )

        self.bT_comf_lb = comfortT_lb
        self.bT_comf_ub = comfortT_ub

        # Build surface azimuth/tilt dicts from component elements (element ids as keys)
        surf_az = {}
        surf_tilt = {}
        for elems in self.component_elements.values():
            for e in elems:
                eid = e.get("id", None)
                if eid is None:
                    continue
                if "azimuth" in e and e["azimuth"] is not None:
                    surf_az[eid] = float(e["azimuth"])
                if "tilt" in e and e["tilt"] is not None:
                    surf_tilt[eid] = float(e["tilt"])

        # compute POA irradiance per element (populates self._irrad_surf in kW/m2)
        self._calcRadiation(surf_az, surf_tilt)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("POA calculated for %d surfaces", len(self._irrad_surf.columns))
            for col in self._irrad_surf.columns[:5]:
                logger.debug(
                    "  %s: max %.3f kW/m2, mean %.3f kW/m2",
                    col, self._irrad_surf[col].max(), self._irrad_surf[col].mean(),
                )
            if len(self._irrad_surf.columns) > 5:
                logger.debug("  ... and %d more surfaces", len(self._irrad_surf.columns) - 5)

        # Build solar gain profiles (kW time series arrays)
        # WINDOWS: each window element may reference a surface (surface field) or be its own surface
        if "g_gl_n_Window" not in self.cfg:
            raise ValueError("g_gl_n_Window (window solar transmittance) must be specified")
        g_gl_default = self.cfg["g_gl_n_Window"]
        if float(g_gl_default) <= 0 or float(g_gl_default) > 1:
            raise ValueError(f"g_gl_n_Window ({g_gl_default}) must be between 0 and 1")

        self.g_gl = float(g_gl_default)

        # Shading and window factors - NO DEFAULTS, must be provided
        if "F_sh_vert" not in self.cfg:
            raise ValueError("F_sh_vert (vertical shading factor) must be specified")
        if "F_sh_hor" not in self.cfg:
            raise ValueError("F_sh_hor (horizontal shading factor) must be specified")
        if "F_w" not in self.cfg:
            raise ValueError("F_w (window frame factor) must be specified")
        if "F_f" not in self.cfg:
            raise ValueError("F_f (floor reflection factor) must be specified")

        self.F_sh_vert = float(self.cfg["F_sh_vert"])
        self.F_sh_hor = float(self.cfg["F_sh_hor"])
        self.F_w = float(self.cfg["F_w"])
        self.F_f = float(self.cfg["F_f"])
        # alpha (absorptance) from constants - NO get() with defaults
        if "alpha" not in self.bConst:
            raise ValueError("Solar absorptance 'alpha' missing from CONST")
        alpha = float(self.bConst["alpha"])

        # windows: POA (kW/m2) * area (m2) * g * fractions -> kW
        win_list = []
        for w in self.windows:
            wid = w.get("id", None)
            if "area" not in w:
                raise ValueError(f"Window element {wid} missing area specification")
            area = float(w["area"])

            # window may reference a parent surface (e.g., "surface": "Wall_1")
            surf_ref = w.get("surface", wid)
            if surf_ref in self._irrad_surf.columns:
                poa = self._irrad_surf[surf_ref].values  # kW/m2
            elif wid in self._irrad_surf.columns:
                poa = self._irrad_surf[wid].values
            else:
                # NO FALLBACK! If POA data missing, that's an error
                raise ValueError(
                    f"POA irradiance data missing for window {wid}"
                    f" (surface: {surf_ref}). Check _calcRadiation."
                )

            gwin = float(w["g_gl"]) if "g_gl" in w else self.g_gl
            # Q [kW] = area * g_gl * irr * fraction factors - small thermal sky term handled below
            qwin = poa * area * gwin * (1.0 - self.F_f) * self.F_w
            win_list.append(qwin)

        if not win_list:
            # A configured-but-empty Windows component is physically valid
            # for a building with zero exposed walls (nowhere to
            # synthesize a window onto) -- zero solar gain, not an error.
            # Consistent with the H_windows=0.0 fallback a few lines below
            # (already handled the "no windows" case for conductance) and
            # with Floor's own "explicitly zero, no solar exposure"
            # treatment further down this method.
            self.profiles["bQ_sol_Windows"] = np.zeros(len(self.times))
        else:
            self.profiles["bQ_sol_Windows"] = np.sum(np.vstack(win_list), axis=0)

        # Window thermal conductance - NO DEFAULTS!
        if "Windows" not in self.bH or "Original" not in self.bH["Windows"]:
            if self.windows:  # Only error if we actually have windows
                raise ValueError("Window conductance H_windows not calculated but windows are present")
            H_windows = 0.0  # No windows = no conductance
        else:
            H_windows = self.bH["Windows"]["Original"]
        # Window sky radiation correction: ISO 13790 §C.4.2
        # Φ_r,win [kW] = H_win [kW/K] × R_se [40 m²K/kW] × h_r [0.0045 kW/m²K] × ΔT_sky [K]
        # Note: R_se × h_r = 40 × 0.0045 = 0.18 (dimensionless product in kW units)
        # Result: ~0.03 kW constant offset per building (small but physically correct)
        thermal_rad_win = H_windows * self.bConst["R_se"] * self.bConst["h_r"] * self.bConst["delta_T_sky"]
        self.profiles["bQ_sol_Windows"] = self.profiles["bQ_sol_Windows"] - float(thermal_rad_win)

        # OPAQUE: Walls, Doors, and Roof — each uses its own component U-value.
        # ISO 13790 §11.3.2 effective solar collecting area of opaque component k:
        #   A_sol,k = α_sol × R_se × U_k × A_k
        # where R_se = 0.04 m²K/W (ISO 6946 exterior surface resistance).
        # Only the fraction R_se × U ≈ 4–7% of absorbed POA enters the building as gain;
        # the remainder leaves from the outer surface by convection.  Without this factor
        # opaque gains are ~15x too large, dominating the cooling load.
        R_se_SI = 0.04  # m²K/W — ISO 6946 Table 1 exterior surface resistance

        wall_q = []
        for e in self.component_elements.get("Walls", []):
            eid = e.get("id", None)
            if "area" not in e:
                raise ValueError(f"Wall element {eid} missing area specification")
            area = float(e["area"])
            if eid in self._irrad_surf.columns:
                poa = self._irrad_surf[eid].values
            else:
                raise ValueError(f"POA irradiance data missing for opaque element {eid}. Check _calcRadiation output.")
            U_wall_SI = self._resolve_opaque_element_u("Walls", e)
            wall_q.append(area * alpha * R_se_SI * U_wall_SI * self.F_sh_vert * poa)

        # Doors are separate from walls so each uses its own U-value
        for e in self.component_elements.get("Doors", []):
            eid = e.get("id", None)
            if "area" not in e:
                raise ValueError(f"Door element {eid} missing area specification")
            area = float(e["area"])
            if eid in self._irrad_surf.columns:
                poa = self._irrad_surf[eid].values
            else:
                raise ValueError(f"POA irradiance data missing for door element {eid}. Check _calcRadiation output.")
            U_door_SI = self._resolve_opaque_element_u("Doors", e)
            wall_q.append(area * alpha * R_se_SI * U_door_SI * self.F_sh_vert * poa)

        if not wall_q:
            raise ValueError("No wall/door elements found but walls are configured. Check wall element definitions.")
        self.profiles["bQ_sol_Walls"] = np.sum(np.vstack(wall_q), axis=0)

        roof_q = []
        for e in self.component_elements.get("Roof", []):
            eid = e.get("id", None)
            if "area" not in e:
                raise ValueError(f"Roof element {eid} missing area specification")
            area = float(e["area"])
            if eid in self._irrad_surf.columns:
                poa = self._irrad_surf[eid].values
            else:
                raise ValueError(f"POA irradiance data missing for roof {eid}. Check _calcRadiation output.")
            U_roof_element_SI = self._resolve_opaque_element_u("Roof", e)
            roof_q.append(area * alpha * R_se_SI * U_roof_element_SI * self.F_sh_hor * poa)
        if not roof_q:
            raise ValueError("No roof elements found but roofs are configured. Check roof element definitions.")
        self.profiles["bQ_sol_Roof"] = np.sum(np.vstack(roof_q), axis=0)

        # Floor solar gains should be explicitly zero (no solar exposure)
        self.profiles["bQ_sol_Floor"] = np.zeros(len(self.times))  # floor solar gains are zero by design
        self.profiles["bQ_sol_Opaque"] = (
            self.profiles["bQ_sol_Walls"]
            + self.profiles["bQ_sol_Roof"]
            + self.profiles["bQ_sol_Floor"]
        )

        # provide debug sums (kWh per timestep is kW * 1h)
        total_window_solar = self.profiles["bQ_sol_Windows"].sum()
        total_opaque_solar = self.profiles["bQ_sol_Opaque"].sum()
        total_wall_solar = self.profiles["bQ_sol_Walls"].sum()
        total_roof_solar = self.profiles["bQ_sol_Roof"].sum()

        logger.debug(
            "Annual solar gains (kWh): windows %.1f, walls %.1f, roof %.1f, opaque total %.1f;"
            " peak windows %.2f kW, peak opaque %.2f kW",
            total_window_solar, total_wall_solar, total_roof_solar, total_opaque_solar,
            self.profiles["bQ_sol_Windows"].max(), self.profiles["bQ_sol_Opaque"].max(),
        )

    def _calcRadiation(self, surf_az: dict, surf_tilt: dict):
        """
        Compute plane-of-array (POA) irradiance for every building surface
        element using pvlib's isotropic sky diffuse model.

        Parameters
        ----------
        surf_az : dict[str, float]
            Element-id → surface azimuth [°] (pvlib convention: 0=N, 180=S).
        surf_tilt : dict[str, float]
            Element-id → surface tilt [°] (0=horizontal-up, 90=vertical).

        Side effects
        ------------
        Populates ``self._irrad_surf`` (DataFrame, cols = element ids,
        values = POA global irradiance in **kW/m²**).

        Notes
        -----
        - Floor elements receive 0 (downward-facing, no solar exposure).
        - Weather series are consumed exactly as supplied: no clipping,
          masking, or range adjustment happens anywhere in this module.
          Weather is the responsibility of its own source -- the
          ``weather`` package for fetched data, and
          ``geojson_validator`` for caller-supplied profiles, which
          range-checks them at the request boundary where a caller can
          still act on the feedback.
        """
        # compute solar position and helpers - NO DEFAULTS for coordinates
        if "latitude" not in self.cfg:
            raise ValueError("Latitude must be specified in configuration for solar calculations")
        if "longitude" not in self.cfg:
            raise ValueError("Longitude must be specified in configuration for solar calculations")

        latitude = float(self.cfg["latitude"])
        longitude = float(self.cfg["longitude"])

        if not (-90 <= latitude <= 90):
            raise ValueError(f"Latitude {latitude} out of valid range [-90, 90]")
        if not (-180 <= longitude <= 180):
            raise ValueError(f"Longitude {longitude} out of valid range [-180, 180]")

        solpos = pvlib.solarposition.get_solarposition(
            self.cfg["weather"].index,
            latitude,
            longitude,
        )
        AM = pvlib.atmosphere.get_relative_airmass(solpos["apparent_zenith"])
        dni_extra = pvlib.irradiance.get_extra_radiation(self.cfg["weather"].index.dayofyear)

        # ensure weather contains DNI/DHI/GHI (pvlib needs them)
        required_weather = ["DNI", "DHI", "GHI", "T"]
        missing = [k for k in required_weather if k not in self.cfg["weather"]]
        if missing:
            available = list(self.cfg['weather'].columns)
            raise RuntimeError(
                f"Weather must include {missing} series for POA"
                f" calculations. Available: {available}"
            )

        weather_data = self.cfg["weather"]

        df = pd.DataFrame(index=self.times)
        for comp, elems in self.component_elements.items():
            for e in elems:
                eid = e.get("id")
                if eid is None:
                    continue
                # Floor faces downward — no direct solar gains; skip POA calculation
                if comp == "Floor":
                    df[eid] = 0.0
                    continue

                # pvlib surface_tilt convention: 0=horizontal-up, 90=vertical, 180=horizontal-down
                # Elements MUST specify tilt in pvlib convention — no silent defaults allowed
                if e.get("tilt") is not None:
                    tilt = float(e["tilt"])
                elif eid in surf_tilt:
                    tilt = float(surf_tilt[eid])
                else:
                    raise ValueError(
                        f"Tilt not specified for element '{eid}' in component '{comp}'. "
                        "Provide 'tilt' in pvlib convention (0=horizontal-up, 90=vertical, 180=horizontal-down)."
                    )

                # Resolve azimuth precedence: element -> surf_az dict -> default (180° south)
                if "azimuth" in e and e["azimuth"] is not None:
                    az = float(e["azimuth"])
                elif eid in surf_az:
                    az = float(surf_az[eid])
                else:
                    # NO DEFAULT azimuth! Must be specified
                    raise ValueError(f"Azimuth not specified for element {eid} and no default available")

                # Use isotropic sky diffuse model: physically bounded at all sun angles
                # ISO 13790 uses isotropic assumption for opaque + window gains
                # Perez/haydavies blow up at low elevation angles (winter Netherlands) due to DNI/cos(zenith) ratio
                total = pvlib.irradiance.get_total_irradiance(
                    surface_tilt=float(tilt),
                    surface_azimuth=float(az),
                    solar_zenith=solpos["apparent_zenith"],
                    solar_azimuth=solpos["azimuth"],
                    dni=weather_data["DNI"],
                    ghi=weather_data["GHI"],
                    dhi=weather_data["DHI"],
                    dni_extra=dni_extra,
                    airmass=AM,
                    model="isotropic",
                )
                # store POA in kW/m2
                df[eid] = total["poa_global"].fillna(0) / 1000.0
        self._irrad_surf = df
        return df

    # -------- design load --------
    def calcDesignHeatLoad(self) -> float:
        """
        Approximate steady-state design heating load [kW].

        Uses the total aggregated conductance ``H_tot`` and an assumed indoor–
        outdoor design temperature difference (default 22.917 K unless
        ``cfg['design_T_min']`` is set).
        """
        # ensure envelope parsed
        if not self.bH:
            self._initEnvelop()
        H_tot = sum(self.bH[c].get("Original", 0.0) for c in self.bH if "Original" in self.bH[c])
        if "design_T_min" not in self.cfg:
            deltaT = 22.917  # Default design temperature difference if not specified
        else:
            deltaT = 22.917 - float(self.cfg["design_T_min"])
        return H_tot * deltaT

    def _addPara(self):
        """
        Initialise all parameters, envelope, 5R1C network, solar profiles, and
        sizing (``maxLoad``).  Also computes big-M bounds for legacy MILP
        formulations.

        Calls :meth:`_initPara`, :meth:`_initEnvelop`, :meth:`_init5R1C`,
        and :meth:`calcDesignHeatLoad`.
        """

        self._initPara()
        self._initEnvelop()
        self._init5R1C()

        # sizing
        if self.maxLoad is None:
            self.bMaxLoad = self.calcDesignHeatLoad()
            self.maxLoad = self.bMaxLoad
        else:
            self.bMaxLoad = self.maxLoad

        # Prepare basic profiles references for other code paths - NO DEFAULTS, must be provided
        if "Q_ig" not in self.cfg:
            raise ValueError("Q_ig (internal gains profile) must be provided in configuration")
        if "occ_nothome" not in self.cfg:
            raise ValueError("occ_nothome (occupancy away profile) must be provided in configuration")
        if "occ_sleeping" not in self.cfg:
            raise ValueError("occ_sleeping (sleeping occupancy profile) must be provided in configuration")

        self.profiles["bQ_ig"] = self.cfg["Q_ig"]
        self.profiles["occ_nothome"] = self.cfg["occ_nothome"]
        self.profiles["occ_sleeping"] = self.cfg["occ_sleeping"]

        # compute big-M bounds for aggregated heat flows (for compatibility)
        self.bM_q = {}
        self.bm_q = {}
        for comp, d in self.bH.items():
            self.bM_q[comp] = {}
            self.bm_q[comp] = {}
            for state, H_val in d.items():
                # conservative bounds based on comfort temps and weather extremes.
                # bT_comf_ub/lb may vary by timestep -- use the widest excursion
                # across the whole array to keep these legacy big-M bounds conservative.
                high = (float(np.max(self.bT_comf_ub)) - (self.cfg["weather"]["T"].min() - 10)) * H_val
                low = (float(np.min(self.bT_comf_lb)) - (self.cfg["weather"]["T"].max() + 10)) * H_val
                self.bM_q[comp][state] = high
                self.bm_q[comp][state] = low

    def _addVariables(self):
        """Declare placeholder dicts for per-timestep variable containers
        (temperatures, heat flows). These are **not** solver decision variables;
        they are populated during post-processing or by legacy code paths."""

        self.bQ_comp = {}  # per-component heat flow [kW]

        # auxiliary variable for thermal mass surface heat flow (legacy)
        self.bP_X = {}

        # temperature dicts (legacy per-timestep containers)
        self.bT_m = {}   # thermal mass node [°C]
        self.bT_air = {}  # air node [°C]
        self.bT_s = {}   # surface node [°C]

        # heat-flow dicts (legacy per-timestep containers)
        self.bQ_ia = {}  # convective gains to air node [kW]
        self.bQ_m = {}   # radiative gains to mass node [kW]
        self.bQ_st = {}  # radiative gains to surface node [kW]

        # ventilation
        self.bQ_ve = {}  # ventilation heat flow [kW]

    def scaleHeatLoad(self, scale=1):
        """
        Multiply original U-values and air-change rates by *scale*.

        On first call the current values are saved; subsequent calls re-scale
        from those saved originals so that ``scaleHeatLoad(1)`` always restores
        the initial state.
        """
        if not hasattr(self, "_orig_U_Values"):
            self._orig_U_Values = {}
            # capture legacy U_* keys
            for key in self.cfg:
                if str(key).startswith("U_"):
                    self._orig_U_Values[key] = self.cfg[key]
            self._orig_U_Values["n_air_infiltration"] = self.cfg.get("n_air_infiltration", 0.0)
            self._orig_U_Values["n_air_use"] = self.cfg.get("n_air_use", 0.0)

        for key, val in self._orig_U_Values.items():
            self.cfg[key] = val * scale

    # -------- constraints & solver --------
    def _addConstraints(self):
        """
        Build the 5R1C physics equality-constraint system for all timesteps.

        Variable layout (n = number of hourly timesteps, typically 8760)::

            x = [T_air(0…n-1), T_m(0…n-1), T_sur(0…n-1), Q_HC(0…n-1)]

        Three equality constraints per timestep (total 3n rows, 4n columns):

        1. **Air-node balance** (Schütz Eq. 22)::

               (H_is + H_ve) T_air − H_is T_sur − Q_HC = φ_ia + H_ve T_e

        2. **Surface-node balance** (Schütz Eq. 21)::

               (H_is + H_ms + H_win) T_sur − H_is T_air − H_ms T_m
                   = φ_st + H_win T_e

        3. **Mass-node forward-Euler dynamics** (Schütz Eq. 20)::

               (C_m/Δt) T_m(i+1) + (−C_m/Δt + H_ms + H_tr_em) T_m(i)
                   − H_ms T_sur(i) = H_tr_em T_e(i) + φ_m(i)

           with wrap-around ``T_m(n−1) → T_m(0)`` for annual periodicity.

        Gain distribution follows ISO 13790 §C.2::

            φ_ia = 0.5 × φ_int                          (convective → air)
            φ_st = f_st × (0.5 × φ_int + φ_sol)       (radiative → surface)
            φ_m  = f_Am × (0.5 × φ_int + φ_sol)       (radiative → mass)

        Comfort bounds ``T_lb ≤ T_air ≤ T_ub`` are applied as LP variable
        bounds in :meth:`sim_model`, not here.

        Returns
        -------
        A_eq : scipy.sparse matrix (3n × 4n)
        b_eq : ndarray (3n,)
        milp_meta : dict
            Compact parameter bundle forwarded to :meth:`_build_and_solve_milp`
            when ``use_milp=True``.
        """
        n = len(self.timeIndex)
        self.n_vars = 4 * n  # [T_air, T_m, T_sur, Q_HC] per timestep
        return self._addConstraints_sequential()

    def _addConstraints_sequential(self):
        """Assemble the 3n × 4n sparse equality system row by row.

        Called by :meth:`_addConstraints`.  See its docstring for the full
        equation reference.  Returns ``(A_eq, b_eq, milp_meta)``.

        Internal gains
        --------------
        ``Q_ia = Q_ig + elecLoad``, where ``Q_ig`` (occupant metabolic
        and equipment-related heat, kW) and ``elecLoad`` (electricity
        load, kW) are both supplied via ``cfg`` already scaled by
        real-time occupant presence — the upstream generator (see
        ``occupancy.core.buem_adapter.to_buem_profiles``) computes
        ``Q_ig`` directly from present/active occupant counts, so it is
        zero whenever the building is unoccupied. ``cfg['occ_nothome']``/
        ``cfg['occ_sleeping']`` are still required inputs (validated in
        :meth:`_addPara`) but are not used to rescale ``Q_ia`` here,
        avoiding a redundant presence discount on an already
        presence-scaled quantity.
        """
        n = len(self.timeIndex)

        # Helper to get variable indices
        def idx_T_air(i): return i
        def idx_T_m(i): return n + i
        def idx_T_sur(i): return 2 * n + i
        def idx_Q_HC(i): return 3 * n + i

        # Prepare equality constraint lists
        eq_rows, eq_vals = [], []

        # Aggregated conductances from self.bH['Original'].
        # Required components raise if missing; optional (Windows, Doors) default to 0.
        for _req in ("Walls", "Roof", "Floor", "Ventilation"):
            if _req not in self.bH or "Original" not in self.bH[_req]:
                raise ValueError(
                    f"{_req} conductance not found in self.bH. "
                    "Check that the component is present in cfg['components'] and that "
                    "_initEnvelop ran successfully."
                )
        H_walls = self.bH["Walls"]["Original"]
        H_roofs = self.bH["Roof"]["Original"]
        H_floors = self.bH["Floor"]["Original"]
        H_ve = self.bH["Ventilation"]["Original"]
        # Windows and Doors are optional:
        H_windows = self.bH["Windows"]["Original"] if "Windows" in self.bH and "Original" in self.bH["Windows"] else 0.0
        H_doors = (
            self.bH["Doors"]["Original"]
            if "Doors" in self.bH and "Original" in self.bH["Doors"]
            else 0.0
        )

        # ISO 13790 §13.2.2: intermittent heating reduction factor.
        # Reduces transmission conductances to account for night/absence setback
        # not explicitly modelled by the hourly comfort bounds.
        # TABULA F_red_htr1: 0.95 (AB/MFH) or 0.90 (SFH/TH); default 1.0 = no reduction.
        F_red = float(self.cfg.get("F_red_htr", 1.0))
        if not (0.0 < F_red <= 1.0):
            raise ValueError(f"F_red_htr must be in (0, 1], got {F_red}")
        H_walls *= F_red
        H_roofs *= F_red
        H_floors *= F_red
        H_windows *= F_red
        H_doors *= F_red

        # Total transmission conductance and opaque-only mass-node conductance.
        H_tot = H_ve + H_walls + H_roofs + H_floors + H_windows + H_doors
        # H_tr_em: mass node couples to exterior through opaque components only
        # (ISO 13790 §12.2.2).  H_ve → air node; H_windows → surface node.
        H_tr_em = H_walls + H_roofs + H_floors + H_doors
        logger.debug(
            "H_tot=%.4f kW/K, H_tr_em=%.4f kW/K (mass node), H_ve=%.4f, H_windows=%.4f",
            H_tot, H_tr_em, H_ve, H_windows,
        )

        # Validate minimum transmission conductance
        if H_tot <= 0.001:  # Less than 1 W/K is unrealistic
            raise ValueError(
                f"Total transmission conductance too low: {H_tot:.6f} kW/K."
                " Check building envelope definition."
            )

        # mass–surface and surface–air conductances
        C_m = self.bC_m
        H_ms = self.bH_ms
        # H_is must have been set by _init5R1C
        if not hasattr(self, "bH_is") or self.bH_is is None:
            raise ValueError("H_is conductance not calculated. Call _init5R1C first.")
        H_is = self.bH_is

        step = self.stepSize

        # ISO 13790 §C.2 gain distribution fractions
        # f_Am: fraction of radiative gains absorbed by thermal mass
        # f_w:  fraction of radiative gains lost directly through windows
        # f_st: remainder reaching internal surfaces
        f_Am = self.bA_m / self.bA_tot
        f_w = H_windows / (self.bConst["h_ms"] * self.bA_tot)  # h_ms in kW/m²K
        f_st = max(0.0, 1.0 - f_Am - f_w)
        logger.debug("ISO 13790 §C.2 gain fractions: f_Am=%.3f, f_st=%.3f, f_w=%.4f", f_Am, f_st, f_w)

        # use precomputed solar profiles from _init5R1C - NO FALLBACKS
        if (
            "bQ_sol_Windows" not in self.profiles
            or "bQ_sol_Opaque" not in self.profiles
            or "bQ_ig" not in self.profiles
        ):
            raise ValueError("Solar/internal gain profiles not initialised. _init5R1C must run first.")
        Q_win_profile = np.asarray(self.profiles["bQ_sol_Windows"])
        Q_opaque_profile = np.asarray(self.profiles["bQ_sol_Opaque"])
        if "occ_nothome" not in self.profiles or "occ_sleeping" not in self.profiles:
            raise ValueError("Occupancy profiles not set in self.profiles. Call sim_model or _addPara first.")

        # Per-timestep gain arrays (also forwarded in milp_meta)
        Q_air_list = np.zeros(n)
        Q_surface_list = np.zeros(n)
        Q_mass_list = np.zeros(n)
        T_e_list = np.zeros(n)

        # Build constraint rows for each timestep
        for i, (t1, t2) in enumerate(self.timeIndex):
            Q_sol_win = float(Q_win_profile[i])
            Q_sol_opaque = float(Q_opaque_profile[i])

            # Internal gains: occupant metabolic/equipment heat plus
            # electrical load, both already scaled by real-time occupant
            # presence upstream (see class docstring). No further
            # presence-based discount is applied here.
            Q_ig = float(self.profiles["bQ_ig"].iloc[i])
            if "elecLoad" not in self.cfg:
                raise ValueError("elecLoad (electricity load profile) must be provided in configuration")
            elecLoad = float(self.cfg["elecLoad"].iloc[i])
            Q_ia = Q_ig + elecLoad

            if isinstance(self.profiles.get("T_e"), dict):
                T_e = self.profiles["T_e"][(t1, t2)]
            else:
                T_e = float(self.cfg["weather"]["T"].iloc[i])

            # ISO 13790 §C.2 gain distribution (Schütz et al. 2017 Eqs. 20-22)
            # φ_int = total internal gains;  φ_sol = total solar gains (window + opaque)
            phi_int = Q_ia
            phi_sol = Q_sol_win + Q_sol_opaque
            phi_ia = 0.5 * phi_int                          # convective → air node
            phi_st = f_st * (0.5 * phi_int + phi_sol)       # radiative → surface node
            phi_m = f_Am * (0.5 * phi_int + phi_sol)        # radiative → mass node

            Q_air = phi_ia
            Q_surface = phi_st

            # Store per-timestep values for milp_meta
            Q_air_list[i] = Q_air
            Q_surface_list[i] = Q_surface
            Q_mass_list[i] = phi_m
            T_e_list[i] = T_e

            # 1) Air node balance (Schütz Eq. 22):
            #   (H_is + H_ve) T_air - H_is T_sur - Q_HC = φ_ia + H_ve T_e
            row = lil_matrix((1, self.n_vars))
            row[0, idx_T_air(i)] = H_is + H_ve
            row[0, idx_T_sur(i)] = -H_is
            row[0, idx_Q_HC(i)] = -1
            eq_rows.append(row)
            eq_vals.append(Q_air + H_ve * T_e)

            # 2) Surface node balance (Schütz Eq. 21):
            #   (H_is + H_ms + H_win) T_sur - H_is T_air - H_ms T_m = φ_st + H_win T_e
            row = lil_matrix((1, self.n_vars))
            row[0, idx_T_sur(i)] = H_is + H_ms + H_windows
            row[0, idx_T_air(i)] = -H_is
            row[0, idx_T_m(i)] = -H_ms
            eq_rows.append(row)
            eq_vals.append(Q_surface + H_windows * T_e)

            # 3) Mass node dynamics (ISO 13790 forward Euler, annual-periodic):
            # C_m/step*(T_m(i+1) - T_m(i)) = φ_m + H_ms*(T_sur(i)-T_m(i)) + H_tr_em*(T_e(i)-T_m(i))
            # Rearranged to A*x=b:
            #   (C_m/step)*T_m(i+1) + (-C_m/step+H_ms+H_tr_em)*T_m(i) + (-H_ms)*T_sur(i) = H_tr_em*T_e + φ_m
            # For i=n-1 the "next" T_m wraps to T_m(0) — this enforces annual periodicity without
            # an explicit initial condition, letting the solver find the self-consistent periodic state.
            if i < n - 1:
                row = lil_matrix((1, self.n_vars))
                row[0, idx_T_m(i+1)] = C_m / step
                row[0, idx_T_m(i)] = -C_m / step + H_ms + H_tr_em
                row[0, idx_T_sur(i)] = -H_ms
                eq_rows.append(row)
                eq_vals.append(H_tr_em * T_e + phi_m)
            else:
                # Wrap-around: same dynamics with T_m(n-1) → T_m(0)
                row = lil_matrix((1, self.n_vars))
                row[0, idx_T_m(0)] = C_m / step
                row[0, idx_T_m(i)] = -C_m / step + H_ms + H_tr_em
                row[0, idx_T_sur(i)] = -H_ms
                eq_rows.append(row)
                eq_vals.append(H_tr_em * T_e + phi_m)

        # --- Assemble equality matrix  A_eq (3*n x 4*n) ---
        A_eq = vstack(eq_rows) if eq_rows else None
        b_eq = np.array(eq_vals) if eq_vals else None

        # milp_meta: parameter bundle forwarded to _build_and_solve_milp.
        # Previously swallowed any failure here into a made-up design=1000.0
        # with no log/warning, silently masking the real error rather than
        # surfacing it -- see CHANGELOG.md. By the time this runs,
        # validate_cfg()/_initEnvelop()/_init5R1C() have already succeeded
        # earlier in the same sim_model() call, so this is not expected to
        # fire in practice -- if it ever does, that itself is worth
        # knowing about, not hiding.
        try:
            design = max(1.0, float(self.calcDesignHeatLoad()))
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning(
                "calcDesignHeatLoad() failed while building MILP big-M "
                "bounds: %s", exc,
            )
            raise ValueError(
                f"Could not compute design heat load for MILP big-M bounds: {exc}"
            ) from exc
        # bT_comf_ub/lb may vary by timestep -- use the widest gap across the array.
        temp_range = max(0.1, float(np.max(np.abs(self.bT_comf_ub - self.bT_comf_lb))))
        M_array = np.zeros(n)
        for i in range(n):
            peak_gain = abs(Q_air_list[i]) + abs(Q_surface_list[i])
            M_array[i] = max(100.0, 2.0 * design, H_tot * temp_range + 2.0 * peak_gain)

        milp_meta = {
            "n": n,
            "H_is": H_is,
            "H_ms": H_ms,
            "H_windows": H_windows,
            "H_ve": H_ve,
            "H_tot": H_tot,
            "H_tr_em": H_tr_em,
            "C_m": C_m,
            "step": step,
            "Q_air": Q_air_list,
            "Q_surface": Q_surface_list,
            "Q_mass": Q_mass_list,
            "T_e": T_e_list,
            "M_array": M_array,
        }

        return A_eq, b_eq, milp_meta

    def _ensure_milp_solver(self):
        """
        Discover an available MILP solver for :meth:`_build_and_solve_milp`.

        Returns
        -------
        (cvxpy_solver | None, cbc_exe_path | None, glpsol_path | None)
            If cvxpy exposes a MILP-capable solver (CBC or GLPK_MI) the first
            element is its ``cp.*`` constant.  Otherwise ``None`` and the caller
            falls back to PuLP with the discovered executable paths.

        Side effects
        ------------
        - Loads ``.env`` variables (``BUEM_CBC_EXE``, ``BUEM_CBC_DIR``).
        - Prepends vendor/softwares directories to ``PATH``.
        """
        pkg_dir = os.path.dirname(__file__)
        # search for .env up to a few levels and load it (python-dotenv)
        cur = os.path.abspath(os.path.join(pkg_dir, ".."))
        found_env = None
        for _ in range(5):
            env_path = os.path.join(cur, ".env")
            if os.path.isfile(env_path):
                found_env = env_path
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if found_env:
            try:
                load_dotenv(found_env, override=False)
                logger.debug("[MILP] Loaded .env: %s", found_env)
            except (OSError, ValueError):
                logger.warning("[MILP] python-dotenv failed to load .env (continuing)")

        def clean_path(p):
            if p is None:
                return None
            s = str(p).strip()
            if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
                s = s[1:-1]
            s = os.path.expandvars(os.path.expanduser(s))
            return os.path.abspath(s)

        cbc_exe_env = clean_path(os.environ.get("BUEM_CBC_EXE"))
        cbc_dir_env = clean_path(os.environ.get("BUEM_CBC_DIR"))

        # candidate dirs (vendor / softwares / env-specified)
        vendor_candidates = [
            os.path.normpath(os.path.join(pkg_dir, "..", "vendors", "cbc")),
            os.path.normpath(os.path.join(pkg_dir, "..", "vendors", "cbc", "bin")),
            os.path.normpath(os.path.join(pkg_dir, "..", "softwares")),
            os.path.normpath(os.path.join(pkg_dir, "..", "softwares", "bin")),
        ]
        if cbc_exe_env:
            vendor_candidates.insert(0, os.path.dirname(cbc_exe_env))
        if cbc_dir_env:
            vendor_candidates.insert(0, cbc_dir_env)

        added_dirs = []
        for d in vendor_candidates:
            if not d:
                continue
            dabs = os.path.abspath(d)
            if os.path.isdir(dabs) and dabs not in os.environ.get("PATH", ""):
                os.environ["PATH"] = dabs + os.pathsep + os.environ.get("PATH", "")
                added_dirs.append(dabs)

        logger.debug("[MILP] Env BUEM_CBC_EXE=%s, BUEM_CBC_DIR=%s", cbc_exe_env, cbc_dir_env)
        if added_dirs:
            logger.debug("[MILP] Added to PATH: %s", added_dirs)

        # locate executables
        cbc_path = (
            shutil.which("cbc")
            or shutil.which("cbc.exe")
            or (cbc_exe_env if cbc_exe_env and os.path.isfile(cbc_exe_env) else None)
        )
        glpsol_path = shutil.which("glpsol") or shutil.which("glpk.exe")

        logger.debug("[MILP] shutil.which -> cbc: %s, glpsol: %s", cbc_path, glpsol_path)
        logger.debug("[MILP] cvxpy.installed_solvers(): %s", cp.installed_solvers())

        # prefer cvxpy enumerated solvers if available
        if "CBC" in cp.installed_solvers():
            return cp.CBC, cbc_path, glpsol_path
        if "GLPK_MI" in cp.installed_solvers():
            return cp.GLPK_MI, cbc_path, glpsol_path

        # otherwise return None and discovered executable paths for external solver usage
        return None, cbc_path, glpsol_path

    def _build_and_solve_milp(self, milp_meta):
        """
        Build and solve the MILP formulation (experimental).

        Separates heating (``Q_heat ≥ 0``) and cooling (``Q_cool ≥ 0``) with a
        binary indicator ``y`` and big-M constraints to prevent simultaneous
        heating and cooling.  Objective: ``min Σ (Q_heat + Q_cool)``.

        Uses cvxpy's built-in MILP solver if available; otherwise falls back to
        PuLP + external CBC executable.

        Parameters
        ----------
        milp_meta : dict
            Parameter bundle from :meth:`_addConstraints` containing
            conductances, gain arrays, temperature arrays, and big-M values.
        """
        n = int(milp_meta["n"])
        H_is = float(milp_meta["H_is"])
        H_ms = float(milp_meta["H_ms"])
        H_windows = float(milp_meta["H_windows"])
        H_ve = float(milp_meta["H_ve"])
        H_tot = float(milp_meta["H_tot"])
        # H_tr_em: opaque-only mass-to-exterior conductance (ISO 13790)
        H_tr_em = float(milp_meta.get("H_tr_em", H_tot))  # fallback to H_tot for old milp_meta
        C_m = float(milp_meta["C_m"])
        step = float(milp_meta["step"])
        Q_air = np.asarray(milp_meta["Q_air"])
        Q_surface = np.asarray(milp_meta["Q_surface"])
        T_e = np.asarray(milp_meta["T_e"])
        M_array = np.asarray(milp_meta.get("M_array", None))
        if M_array is None:
            M_array = np.full(n, float(milp_meta.get("M", 1e4)))

        # build cvxpy model variables (preferred)
        T_air = cp.Variable(n)
        T_m = cp.Variable(n)
        T_sur = cp.Variable(n)
        Q_heat = cp.Variable(n, nonneg=True)
        Q_cool = cp.Variable(n, nonneg=True)
        y = cp.Variable(n, boolean=True)

        constraints = []
        for i in range(n):
            constraints.append(
                (H_is + H_ve) * T_air[i] - H_is * T_sur[i]
                - Q_heat[i] + Q_cool[i]
                == Q_air[i] + H_ve * T_e[i]
            )
            constraints.append(
                (H_is + H_ms + H_windows) * T_sur[i]
                - H_is * T_air[i] - H_ms * T_m[i]
                == Q_surface[i] + H_windows * T_e[i]
            )
            if i == 0:
                constraints.append(T_m[0] == self.T_set)
            elif i < n - 1:
                constraints.append(
                    (-C_m / step - H_ms - H_tr_em) * T_m[i]
                    + (C_m / step) * T_m[i + 1]
                    + H_ms * T_sur[i]
                    == -H_tr_em * T_e[i]
                )
            else:
                constraints.append(T_m[n - 1] - T_m[0] == 0)
            constraints.append(T_air[i] >= self.bT_comf_lb[i])
            constraints.append(T_air[i] <= self.bT_comf_ub[i])
            Mi = float(M_array[i])
            constraints.append(Q_heat[i] <= Mi * y[i])
            constraints.append(Q_cool[i] <= Mi * (1 - y[i]))

        objective = cp.Minimize(cp.sum(Q_heat + Q_cool))
        prob = cp.Problem(objective, constraints)

        solver_enum, cbc_path, _glpsol_path = self._ensure_milp_solver()

        if solver_enum is not None:
            # let cvxpy solve with its solver enum
            prob.solve(solver=solver_enum, verbose=False)
            if prob.status not in ["optimal", "optimal_inaccurate"]:
                raise RuntimeError(f"MILP solve failed (status={prob.status})")
            self.T_air = np.asarray(T_air.value).astype(float)
            self.T_m = np.asarray(T_m.value).astype(float)
            self.T_sur = np.asarray(T_sur.value).astype(float)
            self.Q_heat = np.asarray(Q_heat.value).astype(float)
            self.Q_cool = np.asarray(Q_cool.value).astype(float)
        else:
            # fallback: use PuLP + cbc executable
            try:
                import pulp
            except ImportError:
                raise RuntimeError(
                    "No cvxpy MILP solver available and PuLP"
                    " not installed. Install pulp (pip install pulp)."
                )

            if not cbc_path:
                raise RuntimeError(
                    "No CBC executable found and cvxpy has no MILP solver"
                    " interface. Set BUEM_CBC_EXE in .env or install a"
                    " cvxpy-supported solver."
                )

            # build PuLP model (same constraints)
            prob_pulp = pulp.LpProblem("buem_milp", pulp.LpMinimize)
            T_air_p = [pulp.LpVariable(f"T_air_{i}", lowBound=None, upBound=None, cat="Continuous") for i in range(n)]
            T_m_p = [pulp.LpVariable(f"T_m_{i}", lowBound=None, upBound=None, cat="Continuous") for i in range(n)]
            T_sur_p = [pulp.LpVariable(f"T_sur_{i}", lowBound=None, upBound=None, cat="Continuous") for i in range(n)]
            Q_heat_p = [pulp.LpVariable(f"Q_heat_{i}", lowBound=0, cat="Continuous") for i in range(n)]
            Q_cool_p = [pulp.LpVariable(f"Q_cool_{i}", lowBound=0, cat="Continuous") for i in range(n)]
            y_p = [pulp.LpVariable(f"y_{i}", cat="Binary") for i in range(n)]

            for i in range(n):
                prob_pulp += (
                    (H_is + H_ve) * T_air_p[i] - H_is * T_sur_p[i]
                    - Q_heat_p[i] + Q_cool_p[i]
                    == Q_air[i] + H_ve * T_e[i]
                )
                prob_pulp += (
                    (H_is + H_ms + H_windows) * T_sur_p[i]
                    - H_is * T_air_p[i] - H_ms * T_m_p[i]
                    == Q_surface[i] + H_windows * T_e[i]
                )
                if i == 0:
                    prob_pulp += (T_m_p[0] == self.T_set)
                elif i < n - 1:
                    prob_pulp += (
                        (-C_m / step - H_ms - H_tr_em) * T_m_p[i]
                        + (C_m / step) * T_m_p[i+1]
                        + H_ms * T_sur_p[i]
                        == -H_tr_em * T_e[i]
                    )
                else:
                    prob_pulp += (T_m_p[n-1] - T_m_p[0] == 0)
                prob_pulp += (T_air_p[i] >= self.bT_comf_lb[i])
                prob_pulp += (T_air_p[i] <= self.bT_comf_ub[i])
                Mi = float(M_array[i])
                prob_pulp += (Q_heat_p[i] <= Mi * y_p[i])
                prob_pulp += (Q_cool_p[i] <= Mi * (1 - y_p[i]))

            prob_pulp += pulp.lpSum([Q_heat_p[i] + Q_cool_p[i] for i in range(n)])
            # Use PULP_CBC_CMD without explicit path (we already added the cbc dir to PATH).
            # Passing path to PULP_CBC_CMD may raise "Use COIN_CMD if you want to set a path".
            # If you prefer passing a path, use pulp.COIN_CMD(path=cbc_path) instead.
            solver_cmd = pulp.PULP_CBC_CMD(msg=False)
            res = prob_pulp.solve(solver_cmd)
            status = pulp.LpStatus[res] if isinstance(res, int) else pulp.LpStatus.get(res, res)
            if status not in ("Optimal", "optimal"):
                raise RuntimeError(f"PuLP/CBC solve failed: status={status}")

            self.T_air = np.array([v.value() for v in T_air_p], dtype=float)
            self.T_m = np.array([v.value() for v in T_m_p], dtype=float)
            self.T_sur = np.array([v.value() for v in T_sur_p], dtype=float)
            self.Q_heat = np.array([v.value() for v in Q_heat_p], dtype=float)
            self.Q_cool = np.array([v.value() for v in Q_cool_p], dtype=float)

        self.heating_load = np.maximum(0.0, self.Q_heat)
        self.cooling_load = -np.maximum(0.0, self.Q_cool)

        self._readResults()

    def sim_model(self, use_milp: bool = False):
        """
        Run the ISO 13790 single-pass dead-band simulation.

        Builds the 5R1C equality system via :meth:`_addConstraints` and solves
        a Linear Programme (LP) with an L1-norm objective::

            min  Σ |Q_HC(i)|          (total HVAC energy)
            s.t. A_eq · x = b_eq     (physics)
                 T_lb ≤ T_air ≤ T_ub  (dead-band comfort)

        The L1 norm is reformulated internally by cvxpy into a standard LP
        (auxiliary variables t_i ≥ ±Q_HC_i).  This produces **sparse**
        solutions: Q_HC = 0 whenever indoor temperature can remain within the
        comfort band passively — matching real thermostat dead-band behaviour.

        Solver priority: CLARABEL (interior-point) → OSQP (ADMM fallback).

        After solving, near-zero Q_HC values (|Q_HC| < 1 W) are snapped to
        exactly zero to eliminate solver numerical noise.

        Parameters
        ----------
        use_milp : bool, optional
            If *True*, delegates to :meth:`_build_and_solve_milp` instead of
            the LP path.  Default *False*.

        Side effects
        ------------
        Sets ``self.T_air``, ``self.T_m``, ``self.T_sur``, ``self.Q_HC``,
        ``self.heating_load``, ``self.cooling_load``, and populates
        ``self.detailedResults``.
        """
        issues = validate_cfg(self.cfg)
        if issues:
            raise ValueError("Configuration validation failed: " + "; ".join(issues))

        self._initPara()
        self._initEnvelop()
        self._init5R1C()

        self.timeIndex = [(1, t) for t in range(len(self.times))]
        timediff = self.times[1] - self.times[0]
        self.stepSize = timediff.total_seconds() / 3600

        # Ensure occupancy/gain profiles are available
        for key in ("bQ_ig", "occ_nothome", "occ_sleeping"):
            if key not in self.profiles:
                cfg_key = key.replace("b", "", 1) if key.startswith("b") else key
                if cfg_key not in self.cfg:
                    raise ValueError(f"{cfg_key} profile missing from cfg")
                self.profiles[key] = self.cfg[cfg_key]
        if "T_e" not in self.profiles:
            self.profiles["T_e"] = self.cfg["weather"]["T"]

        # T_set: initial mass-node temperature (dead-band midpoint). A single
        # scalar regardless of whether bT_comf_lb/ub vary by timestep -- it only
        # seeds the periodic wrap-around's T_m(0), not a per-timestep bound.
        assert self.bT_comf_lb is not None and self.bT_comf_ub is not None
        self.T_set = float(np.mean((self.bT_comf_lb + self.bT_comf_ub) / 2.0))

        # Build 5R1C physics constraint matrix A_eq (3*n x 4*n)
        A_eq, b_eq, milp_meta = self._addConstraints()

        if use_milp:
            return self._build_and_solve_milp(milp_meta)

        # ── Single-pass ISO 13790 dead-band LP ──────────────────────────────────
        # Variables: x = [T_air(0..n-1), T_m(0..n-1), T_sur(0..n-1), Q_HC(0..n-1)]
        # Equality:  3 physics equations per timestep
        # Bounds:    comfortT_lb <= T_air <= comfortT_ub  (dead-band comfort constraint)
        # Objective: minimize sum(|Q_HC|)  (L1 norm → LP)
        n = len(self.timeIndex)
        x = cp.Variable(4 * n)
        # L1 objective: minimize total |Q_HC| (= total HVAC energy).
        # Sparse solution: Q_HC = 0 (dead-band) whenever physics admits T_air ∈ [lb, ub].
        # This matches ISO 13790 annual energy accounting (total kWh, not peak kW).
        obj = cp.Minimize(cp.norm1(x[3*n:4*n]))
        constraints = [
            A_eq @ x == b_eq,
            x[0:n] >= self.bT_comf_lb,
            x[0:n] <= self.bT_comf_ub,
        ]
        prob = cp.Problem(obj, constraints)
        # ASCII-only: a non-cp1252 character here (e.g. U+2208 "element of")
        # raises UnicodeEncodeError and aborts the solve on a default Windows
        # console, which doesn't set stdout to UTF-8.
        logger.debug(
            "Solving LP: %d vars, A_eq %s, comfort lb in [%.1f,%.1f], ub in [%.1f,%.1f] degC",
            4 * n, A_eq.shape,
            self.bT_comf_lb.min(), self.bT_comf_lb.max(),
            self.bT_comf_ub.min(), self.bT_comf_ub.max(),
        )
        # Try CLARABEL (interior-point, high accuracy) first; fall back to OSQP
        try:
            prob.solve(solver=cp.CLARABEL, verbose=False)
            solver_used = "CLARABEL"
        except (cp.error.SolverError, ValueError):
            prob.solve(solver=cp.OSQP, eps_abs=1e-6, eps_rel=1e-6, max_iter=10000, verbose=False)
            solver_used = "OSQP"
        logger.debug("Solver: %s, status: %s", solver_used, prob.status)
        if prob.status not in ["optimal", "optimal_inaccurate"]:
            raise RuntimeError(
                f"LP solver failed (status={prob.status}, solver={solver_used}). "
                "Check building parameters (U-values, areas) and comfort bounds."
            )

        x_val = np.asarray(x.value)
        self.T_air = x_val[0:n]
        self.T_m = x_val[n:2*n]
        self.T_sur = x_val[2*n:3*n]
        self.Q_HC = x_val[3*n:4*n]

        # Snap near-zero Q_HC to exactly zero (numerical noise in dead-band hours)
        self.Q_HC[np.abs(self.Q_HC) < 1e-3] = 0.0  # |Q_HC| < 1 W → 0

        # Split net HVAC by sign: positive = heating, negative = cooling
        self.heating_load = np.maximum(0.0, self.Q_HC)
        self.cooling_load = np.minimum(0.0, self.Q_HC)

        self._readResults()
        return

    def _addDhwCooking(self):
        """
        Post-process domestic-hot-water / gas-cooking energy from
        occupancy's optional ``dhw_liters``/``cooking_active`` cfg keys.

        Additive alongside ``heating_load`` -- never fed into the 5R1C
        solve above (``_addConstraints_sequential``/``sim_model``'s LP has
        already run by the time this is called). See
        `buem.thermal.dhw_cooking` for the conversion method and its
        sourcing; this method is just the wiring.

        Both cfg keys are **optional**, unlike ``Q_ig``/``elecLoad``/
        ``occ_nothome``/``occ_sleeping`` -- a cfg without them (e.g. a
        service building, which occupancy's DHW model does not cover, or
        any caller/test built before this feature existed) sees no
        behavior change: ``self.dhw_kWh``/``self.cooking_gas_kWh`` simply
        stay ``None``.
        """
        self.dhw_kWh: pd.Series | None = None
        self.cooking_gas_kWh: pd.Series | None = None

        if self.cfg.get("include_dhw", True):
            # Prefer the per-fixture-priced series: occupancy resolves each
            # draw's fixture and they are delivered at different
            # temperatures, so pricing the blended total at one delta-T
            # misprices every draw but the one it matches. Falls back to
            # that blended conversion for a cfg carrying only liters.
            dhw_kwh = self.cfg.get("dhw_kwh")
            dhw_liters = self.cfg.get("dhw_liters")
            if isinstance(dhw_kwh, pd.Series):
                self.dhw_kWh = dhw_kwh.rename("dhw_kWh")
            elif isinstance(dhw_liters, pd.Series):
                self.dhw_kWh = dhw_cooking.dhw_energy_kwh(dhw_liters)

        # Carrier decides whether cooking is reported as its own (gas)
        # term at all. occupancy models cooking appliances electrically,
        # so an electrically-cooking household's cooking energy is already
        # inside elecLoad and reporting it again would double-count it in
        # any total-energy sum.
        carrier = str(self.cfg.get("cooking_carrier", "gas")).lower()
        if carrier == "gas":
            self.cooking_gas_kWh = self._cooking_energy_kwh()

    def _cooking_energy_kwh(self) -> pd.Series | None:
        """Hourly cooking energy, preferring occupancy's own per-appliance
        draws over a distributed annual figure.

        ``cooking_kwh`` comes from occupancy's stochastic model restricted
        to its kitchen appliances, so both the timing and the magnitude of
        each cooking event follow what that household actually owns and
        does. Where it is absent -- an older cfg, or a caller that supplies
        only the boolean ``cooking_active`` flag -- a per-household
        reference total is spread across the flagged hours instead, which
        keeps the timing real but not the magnitude.

        Neither path depends on ``self.heating_load``: deriving cooking
        from simulated heating, as an earlier convention did, made the
        cooking figure inherit any heating error, so a building
        overestimating heating also reported overestimated cooking and the
        two could never be separated against measured gas.
        """
        cooking_kwh = self.cfg.get("cooking_kwh")
        if isinstance(cooking_kwh, pd.Series):
            return cooking_kwh.rename("cooking_gas_kWh")

        cooking_active = self.cfg.get("cooking_active")
        if not isinstance(cooking_active, pd.Series):
            return None
        # Scaled by residential_units so a multi-dwelling block gets every
        # dwelling's cooking, matching how Q_ig/elecLoad are already
        # scaled. cooking_kwh above is scaled upstream, in AttributeBuilder.
        units = max(float(self.cfg.get("residential_units") or 1.0), 1.0)
        return dhw_cooking.cooking_gas_energy_kwh(
            cooking_active,
            annual_total_kwh=dhw_cooking.COOKING_ANNUAL_KWH_PER_HOUSEHOLD * units,
        )

    def _readResults(self):
        """
        Populate ``self.detailedResults`` DataFrame and legacy plotting attributes.

        Columns: Heating Load, Cooling Load, T_air, T_sur, T_m, T_e,
        Electricity Load, DHW Load, Cooking Gas Load.  Also sets
        ``Q_sol_win_series`` and ``Q_sol_opaque_series`` for downstream
        plotting, calls :meth:`diagnostics_solar_components`, and calls
        :meth:`_addDhwCooking` for the two DHW/cooking columns.
        """
        self._addDhwCooking()

        self.detailedResults = pd.DataFrame({
            "Heating Load": self.heating_load,
            "Cooling Load": self.cooling_load,
            "T_air": self.T_air,
            "T_sur": self.T_sur,
            "T_m": self.T_m,
            "T_e": self.cfg["weather"]["T"].values,
            "Electricity Load": self.cfg["elecLoad"].values if "elecLoad" in self.cfg else None,
            "DHW Load": self.dhw_kWh.values if self.dhw_kWh is not None else None,
            "Cooking Gas Load": self.cooking_gas_kWh.values if self.cooking_gas_kWh is not None else None,
        }, index=[t for t in self.timeIndex]
        )
        # Legacy/plotting-friendly attributes expected by standard_plots
        self.Q_sol_win_series = np.asarray(self.profiles.get("bQ_sol_Windows", np.zeros(len(self.times))))
        self.Q_sol_opaque_series = np.asarray(self.profiles.get("bQ_sol_Opaque", np.zeros(len(self.times))))
        logger.debug(
            "Solar gains: windows %.2f kWh, opaque %.2f kWh",
            self.Q_sol_win_series.sum(), self.Q_sol_opaque_series.sum(),
        )

        # Ensure temperature arrays exist as 1D numpy arrays (aliases used by plotting)
        self.T_air = np.asarray(self.T_air)
        self.T_m = np.asarray(self.T_m)
        self.T_sur = np.asarray(self.T_sur)

        det = self.diagnostics_solar_components()
        logger.debug("Diagnostic solar components: %s", det)

    def diagnostics_solar_components(self):
        """
        Return a per-component diagnostic summary, also emitted at DEBUG level.

        For each component reports: total area [m²], mean POA [kW/m²],
        conductance H [kW/K], ``H×R_se``, sky thermal radiation correction
        [kW], and annual solar gain profile sum [kWh].

        Returns
        -------
        dict[str, dict]
            Nested dict keyed by component name.
        """
        det = {}
        R_se = float(self.bConst.get("R_se", 0.0))
        h_r = float(self.bConst.get("h_r", 0.0))
        delta_T_sky = float(self.bConst.get("delta_T_sky", 0.0))
        n = len(self.times)

        for comp, elems in self.component_elements.items():
            areas = [float(e.get("area", 0.0)) for e in elems]
            total_area = float(np.sum(areas)) if areas else 0.0

            # area-weighted mean POA (kW/m2)
            poa_vals = []
            for e in elems:
                eid = e.get("id")
                if eid in self._irrad_surf.columns:
                    poa_vals.append(float(self._irrad_surf[eid].mean()))
            mean_poa = float(np.mean(poa_vals)) if poa_vals else 0.0

            # H (aggregated conductance) and derived terms
            H_comp = float(self.bH.get(comp, {}).get("Original", 0.0))
            H_times_Rse = H_comp * R_se
            thermal_rad = H_comp * h_r * R_se * delta_T_sky

            # profile-based solar (kWh/year) if available in profiles
            # Note: Doors are vertical opaque elements; their solar gains are included in bQ_sol_Walls.
            # The Doors row shows mean_poa and area correctly but profile_sum is reported under Walls.
            profile_key = {
                "Windows": "bQ_sol_Windows",
                "Walls": "bQ_sol_Walls",
                "Roof": "bQ_sol_Roof",
                "Floor": "bQ_sol_Floor",
            }.get(comp, None)
            profile_sum = float(np.sum(self.profiles.get(profile_key, np.zeros(n)))) if profile_key else 0.0

            det[comp] = {
                "total_area_m2": total_area,
                "mean_poa_kW_m2": mean_poa,
                "H_kW_per_K": H_comp,
                "H_times_R_se": H_times_Rse,
                "thermal_rad_kW": thermal_rad,
                "profile_sum_kWh": profile_sum,
            }

        if logger.isEnabledFor(logging.DEBUG):
            for comp, info in det.items():
                logger.debug(
                    " - %s: area=%.1f m2, mean_poa=%.4f kW/m2, H=%.4f kW/K, H*R_se=%.4f,"
                    " thermal_rad=%.4f kW, profile_sum=%.2f kWh",
                    comp, info["total_area_m2"], info["mean_poa_kW_m2"], info["H_kW_per_K"],
                    info["H_times_R_se"], info["thermal_rad_kW"], info["profile_sum_kWh"],
                )
            windows_sum = float(np.sum(self.profiles.get("bQ_sol_Windows", np.zeros(n))))
            opaque_sum = float(np.sum(self.profiles.get("bQ_sol_Opaque", np.zeros(n))))
            logger.debug(
                " GLOBAL: windows_total_kWh=%.2f, opaque_total_kWh=%.2f", windows_sum, opaque_sum,
            )
        return det
