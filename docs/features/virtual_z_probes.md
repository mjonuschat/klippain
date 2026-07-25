# Virtual-Z Probe Framework

Klippain supports probes that act as the Z endstop through concrete probe profile includes. Users still select one probe include in `user_templates/printer.cfg`; the framework underneath standardizes contact temperature guarding, Z-home dispatch, and generic start-print actions.

## Probe Profiles

Use one probe include:

```ini
[include config/hardware/probes/voron_tap.cfg]
[include config/hardware/probes/revo_pz.cfg]
[include config/hardware/probes/beacon_contact.cfg]
[include config/hardware/probes/cartographer_touch.cfg]
```

`voron_tap.cfg` keeps the legacy internal identifier `probe_type_enabled: "vorontap"` for compatibility.
`revo_pz.cfg` is a standard Klipper `[probe]` profile with `probe_contact_z_home_mode: "standard_g28"`, plus Revo PZ-specific per-probe current handling in `[probe] activate_gcode` and `deactivate_gcode`.
`cartographer_touch.cfg` uses standard Cartographer scan-mode `G28` for initial homing, then runs `contact_z_home` during `START_PRINT` after tilt calibration and before bed mesh.

## Shared Contact Variables

Preferred variables:

```ini
variable_probe_contact_max_temp: 150
variable_probe_contact_deactivation_zhop: 5
variable_probe_unsupported_contact_action_policy: "warn"
```

Legacy variables remain supported for existing configs:

```ini
variable_tap_max_probing_temp: 150
variable_tap_deactivation_zhop: 5
variable_beacon_max_probing_temp: 150
variable_beacon_deactivation_zhop: 5
```

If both shared and legacy variables are set, the shared variable wins. With verbose output enabled, Klippain reports the override at startup.

## Start-Print Actions

Generic actions:

```ini
contact_z_home
contact_auto_calibrate
```

`contact_z_home` runs a probe-supported contact Z-home operation. `contact_auto_calibrate` runs a probe-supported contact model calibration, such as Beacon Contact autocalibration. Cartographer Survey Touch does not run `CARTOGRAPHER_TOUCH_CALIBRATE` during normal start print; that remains a user-initiated setup operation.

Profiles can keep normal homing separate from the `START_PRINT` contact operation. `probe_contact_z_home_mode` controls homing/manual contact-home dispatch. `probe_contact_z_home_startprint_mode` controls the `START_PRINT` `contact_z_home` action and defaults to `probe_contact_z_home_mode` when unset. Cartographer Touch sets:

```ini
variable_probe_contact_z_home_mode: "none"
variable_probe_contact_z_home_startprint_mode: "hook"
```

This lets initial `G28` use Cartographer's virtual endstop scan mode, then calls `CARTOGRAPHER_TOUCH_HOME` through the Cartographer hook after tilt calibration and before bed mesh.

contact_z_home fails closed when the active probe profile does not support hook-based contact Z-home. contact_auto_calibrate warns and no-ops by default when unsupported, or raises when this policy is set to `error`:

```ini
variable_probe_unsupported_contact_action_policy: "error"
```

## Guard Recovery

If a guarded contact operation is interrupted (print error, cancel, emergency stop), the temperature guard state is cleared automatically the next time it matters: `START_PRINT`, `CANCEL_PRINT`, `END_PRINT`, the `virtual_sdcard` error handler, and `ACTIVATE_PROBE` all discard a stale guard silently. These resets never move the toolhead and never change the extruder target; `START_PRINT` re-establishes its temperatures itself.

`_PROBE_RECOVER_CONTACT_GUARD` remains available for manual inspection. Run it to see the saved extruder target, then run `_PROBE_RECOVER_CONTACT_GUARD CONFIRM=1` to restore that target once the nozzle is clear of the bed.

With Klippain verbose mode enabled, guard enter, exit, and reset events are reported in the console to help diagnose probing issues.

## Developer Notes

To add a new virtual-Z contact probe, create a concrete profile in `config/hardware/probes/`. Set capability variables and include a hook file only when the probe needs product-specific commands.

Valid operation modes:

- `none`
- `standard_g28`
- `hook`

Hook macros receive `SOURCE`, which is one of `homing`, `start_print`, or `manual`.

`standard_g28` is for Z-home dispatch. Auto-calibration support should use `hook` or `none`.
