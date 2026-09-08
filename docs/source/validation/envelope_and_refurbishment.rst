Envelope, Refurbishment State and the CBS Heating Gap
=====================================================

Population-complete validation of Loenen against CBS, decomposing the
buem-vs-CBS heating discrepancy into its contributing causes. Produced
2026-09-01.

Configuration
-------------

.. code-block:: text

   buem.analysis.batch --source csv \
       --data-dir src/buem/data/buildings/netherlands/Loenen \
       --country NL --region-code GM0200 \
       --year 2018 --provider merra-2 --residential-only

1,335 residential buildings, 0 errors. occupancy 6.0.0. Reference: CBS
85140NED 2019 (national, resolved by dwelling type *and* construction
era). buem's weather year is 2018, so a one-year offset applies.

Two properties of the reference matter when reading every table below.
CBS's era-resolved table is **national**; municipal figures exist only
without era detail (table 81528NED). And Dutch gas use fell steeply over
this period — 2024 heating runs 0.69–0.80× its 2019 level — so the CBS
year must match the simulated weather year or the comparison measures
the weather rather than the model.

Headline: the gap is a refurbishment-state gap
----------------------------------------------

Splitting the population by the TABULA refurbishment variant buem
assigned separates it almost completely:

.. list-table::
   :header-rows: 1

   * - Variant
     - n
     - Share
     - buem kWh/dwelling
     - CBS kWh/dwelling
     - Ratio
   * - 1 — as-built
     - 935
     - 70.0%
     - 47,144
     - 11,667
     - **4.04**
   * - 2 — standard refurbishment
     - 317
     - 23.7%
     - 11,371
     - 11,272
     - **1.01**
   * - 3 — nZEB refurbishment
     - 83
     - 6.2%
     - 6,328
     - 12,454
     - **0.51**

Where buem models a refurbished envelope it reproduces CBS almost
exactly. The aggregate ratio of 3.11 is carried entirely by the as-built
bucket.

The mechanism is data coverage rather than physics. **609 of 1,335
buildings (46%) carry no energy label**, and every one is assigned
variant 1. Among the 726 that do carry a label the split is 326 / 317 /
83 across variants 1 / 2 / 3.

Correcting for coverage alone does not close the gap. Giving the
unlabelled buildings the labelled population's own variant mix
(45/44/11) implies an aggregate near 2.3, because labelled-as-built
buildings are themselves at 4.04.

Where the as-built loss sits
----------------------------

Envelope conductance for a representative as-built pre-1964 terraced
house:

.. list-table::
   :header-rows: 1

   * - Component
     - Area m²
     - H W/K
     - Share
     - U_eff
   * - Roof
     - 102.0
     - 463.5
     - **37.3%**
     - 4.55
   * - Floor
     - 101.9
     - 339.7
     - **27.4%**
     - 3.33
   * - Wall
     - 163.1
     - 220.5
     - 17.8%
     - 1.35
   * - Window
     - 41.1
     - 213.8
     - 17.2%
     - 5.20

Total 1,241.5 W/K over 204 m² floor area — **6.09 W/K per m²**, against
a real Dutch terraced house nearer 1.5–3. Roof and floor together are
65% of the loss. Walls are not the problem: party walls correctly carry
``b_transmission = 0``, pulling the effective wall U to 1.35 against the
reference table's 5.26.

This is the same finding as above, seen mechanically. Roof insulation is
the commonest Dutch retrofit by a wide margin and is the single largest
term here, so an archetype assuming an uninsulated roof describes a
building that has largely stopped existing.

Isolating the envelope assumptions
----------------------------------

Three as-built TH NL.01 buildings against a CBS reference of 10,458
kWh/dwelling:

.. list-table::
   :header-rows: 1

   * - Scenario
     - kWh/dwelling
     - Ratio
   * - As shipped — single glazing, solid-wall opaque values
     - 68,766
     - 6.58
   * - \+ uninsulated-cavity walls, roof, floor
     - 51,323
     - 4.91
   * - \+ HR++ glazing only
     - 59,809
     - 5.72
   * - \+ both
     - **40,434**
     - **3.87**

Glazing alone is worth 13%, the opaque corrections 25%, both together
41% — substantial, and still leaving the building at 3.87×. Two specific
defects were found in the as-built reference table and are included in
the "opaque" scenario:

- Pre-1964 terraced houses used solid-wall values (R_c 0.19 / 0.22 /
  0.15) while other types of the same era used uninsulated-cavity ones
  (0.36 / 0.39 / 0.32). Both are genuine NTA 8800 figures, but cavity
  construction was dominant in Dutch housing well before 1964.
- The 1965–1974 floor R_c was 0.17, *worse* than the pre-1964 value of
  0.32. Construction quality did not regress.

Effect of modelling glazing by era
----------------------------------

Assigning each construction era a realistic glazing class — ``single``
for pre-1975 as-built, ``double_uncoated`` for 1975–1991, ``HR`` for
1992–2005, ``HR++`` for 2006 onward — and re-running the full population:

.. list-table::
   :header-rows: 1

   * - Type / era
     - Before
     - After
     - Change
   * - SFH 1975–1991
     - 2.65
     - **2.01**
     - −24%
   * - SFH 1992–2005
     - 2.08
     - **1.72**
     - −17%
   * - SFH 2006–2014
     - 1.95
     - **1.56**
     - −20%
   * - TH 1975–1991
     - 2.50
     - **2.06**
     - −18%
   * - TH 1992–2005
     - 2.09
     - **1.79**
     - −14%
   * - SFH ≤1964 (control, still single)
     - 3.46
     - 3.45
     - ~0

Count-weighted 3.11 → **2.97**; median simulated heat per dwelling
29,265 → 23,686 kWh. Every era modelled with realistic glazing now sits
at 1.4–2.1. The weighted figure moves less than the individual cells
because the two oldest classes — 870 of 1,335 buildings — still carry
single glazing as-built, which is the refurbishment-coverage problem
above rather than a glazing one.

Energy labels do not determine component U-values
-------------------------------------------------

buem treats a label as a proxy for refurbishment state, mapping it to a
variant whose measures then set U-values. That mapping is one-to-many in
reality: two 'A'-labelled terraced houses of similar construction year in
different regions were observed with wall R_c of **1.97 and 3.5** — a 78%
spread within one label class.

A label is a whole-building performance rating (kWh/m²/yr) integrating
envelope, installation, ventilation and renewables. Two buildings reach
the same rating by different routes — one well-insulated with a modest
boiler, another moderately insulated with a heat pump and PV — so nothing
in the label constrains any individual component. A direct
label-to-U-value table would encode a spread this wide as a point value.

The consequence is that even complete label coverage would leave
substantial per-building error. This bounds how far any label-driven
stock model can go, not just buem's.

Electricity
-----------

The electricity comparison runs the opposite way, at a count-weighted
ratio of **0.72**:

.. list-table::
   :header-rows: 1

   * - Type
     - buem kWh/dwelling
     - CBS range
     - Ratio range
   * - SFH
     - 2,327
     - 3,560–4,675
     - 0.53–0.65
   * - TH
     - 2,535
     - 2,630–3,290
     - 0.77–0.96
   * - MFH / AB
     - 1,649
     - 1,780–2,080
     - 0.79–0.93

buem gives every dwelling of a type the same electricity regardless of
floor area, while the CBS figure rises with dwelling size, so the SFH
miss is structural rather than a level error. Both gaps point the same
way: too little internal gain and too much envelope loss.

Reproducing these results
-------------------------

.. code-block:: text

   python scripts/compare_era_type_vs_cbs.py results/loenen_gm0200.parquet \
       --period 2019JJ00 --by-refurbishment

The reference tables the envelope scenarios vary are user-editable and
live under ``src/buem/data/reference/``: ``glazing_reference.csv`` for
window U- and g-values by glazing class, and
``num_persons_by_building_type.csv`` for household occupancy. Per-region
envelope values live alongside each region's building data as
``u_value_reference.csv``.
