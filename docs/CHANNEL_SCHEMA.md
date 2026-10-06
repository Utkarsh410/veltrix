# Channel schema

Generated from `src/veltrix_engine/schema.py` by `tools/gen_schema_doc.py`. Edit the code, not this file.

Every importer maps its source columns onto these channel ids, and every analysis
module reads and writes only these ids.

## Conventions

Vehicle axes follow ISO 8855: x forward, y left, z up.

| Quantity | Convention |
|---|---|
| Longitudinal acceleration | +ve accelerating, −ve braking |
| Lateral acceleration | +ve in left-hand corners |
| Steering | +ve steering left; degrees at the steering wheel |
| Suspension travel | +ve in compression |
| Position | metres in a flat local plane; orientation arbitrary |
| Corners | `fl`, `fr`, `rl`, `rr` — left and right as seen by the driver |
| Time base | uniform after normalisation; step channels (`lap`, `gear`) are held, never interpolated |
| Lap distance | metres from the start/finish line, scaled so every full lap has the same length |

A file whose signs differ is imported with `flip=("steering", "acc_lat", ...)`.
The engine never guesses a sign.

## Three layers

| Layer | What it is | Where |
|---|---|---|
| Raw | The file as read: source names, source units, untouched | `Session.raw` |
| Normalised | Schema ids, schema units and signs, uniform time base | `Session.channels` |
| Derived | Computed by the engine from normalised channels | `Session.derived` |

## Units accepted on import

Units are read from the file's units row. Anything converted or assumed is recorded
in `Session.notes`.

| Quantity | Stored as | Converted from |
|---|---|---|
| time | s | ms |
| distance | m | km, mi, mile, miles, ft |
| speed | km/h | mph, m/s, kn, kt, knots |
| accel | g | m/s^2, m/s2, m/s/s, ms^2 |
| angle | deg | rad |
| yaw_rate | rad/s | deg/s, °/s |
| wheel_speed | km/h | mph, m/s |
| pressure | bar | psi, kpa, mpa |
| length | mm | cm, m, in, inch |
| position | m | km, ft |
| temperature | °C | °F, K |
| pedal position | % | 0–1 fraction |

Without a units row the engine assumes the stored unit, except where the values make another unit obvious (accelerations above 4 → m/s², tyre pressures above 10 → psi, temperatures of 120–260 → °F, speed checked against distance). Each such guess is a warning.

## Measured channels

| Channel | Unit | Description | Sign | Example source names |
|---|---|---|---|---|
| `time` | s | Session clock |  | time, session time, elapsed time |
| `distance` | m | Distance travelled; cumulative over the session |  | distance, lap distance, lap dist |
| `lap` | - | Lap number as logged |  | lap, lap number, lap num |
| `speed` | km/h | Vehicle ground speed |  | speed, ground speed, vehicle speed |
| `rpm` | rpm | Engine speed |  | rpm, engine rpm, engine speed |
| `gear` | - | Selected gear |  | gear, gear pos, gear position |
| `throttle` | % | Throttle pedal position, 0-100 |  | throttle, throttle pos, throttle position |
| `brake` | % or bar | Brake pedal position (%) or line pressure (bar); the session records which |  | brake, brake pres front, brake press front |
| `steering` | deg | Steering wheel angle | +ve steering left | steering, steering angle, steered angle |
| `acc_lat` | g | Lateral acceleration | +ve in left-hand corners | lat accel, lateral accel, lateral acceleration |
| `acc_long` | g | Longitudinal acceleration | +ve accelerating | long accel, longitudinal accel, longitudinal acceleration |
| `acc_vert` | g | Vertical acceleration | +ve upward | vert accel, vertical accel, vertical acceleration |
| `yaw_rate` | rad/s | Yaw rate from a gyro | +ve turning left | yaw rate, yaw velocity, gyro yaw velocity |
| `tc_active` | - | Traction control intervening (0 / 1) |  | tc, traction control, tc active |
| `abs_active` | - | ABS intervening (0 / 1) |  | abs, abs active |
| `pos_x` | m | Position in the local track plane, x |  | pos x, position x, world position x |
| `pos_y` | m | Position in the local track plane, y |  | pos y, position y, y pos |
| `gps_lat` | deg | GPS latitude |  | gps latitude, latitude, gps lat |
| `gps_lon` | deg | GPS longitude |  | gps longitude, longitude, gps lon |
| `tyre_temp_fl` | °C | Tyre temperature FL, single sensor (surface or core air) |  | tyre temp fl, fl tyre temp, tyre temp lf |
| `tyre_temp_fl_inner` | °C | Tyre surface temperature FL, inner zone |  | tyre temp fl inner, fl tyre temp inner, tyre temp fl inside |
| `tyre_temp_fl_mid` | °C | Tyre surface temperature FL, mid zone |  | tyre temp fl mid, fl tyre temp mid, tyre temp fl middle |
| `tyre_temp_fl_outer` | °C | Tyre surface temperature FL, outer zone |  | tyre temp fl outer, fl tyre temp outer, tyre temp fl outside |
| `brake_temp_fl` | °C | Brake disc temperature FL |  | brake temp fl, fl brake temp, brake temp lf |
| `wheel_speed_fl` | km/h | Wheel speed FL |  | wheel speed fl, fl wheel speed, wheel speed lf |
| `tyre_press_fl` | bar | Tyre pressure FL |  | tyre press fl, fl tyre press, tyre press lf |
| `susp_travel_fl` | mm | Suspension travel FL | +ve in compression | susp travel fl, fl susp travel, susp travel lf |
| `tyre_temp_fr` | °C | Tyre temperature FR, single sensor (surface or core air) |  | tyre temp fr, fr tyre temp, tyre temp rf |
| `tyre_temp_fr_inner` | °C | Tyre surface temperature FR, inner zone |  | tyre temp fr inner, fr tyre temp inner, tyre temp fr inside |
| `tyre_temp_fr_mid` | °C | Tyre surface temperature FR, mid zone |  | tyre temp fr mid, fr tyre temp mid, tyre temp fr middle |
| `tyre_temp_fr_outer` | °C | Tyre surface temperature FR, outer zone |  | tyre temp fr outer, fr tyre temp outer, tyre temp fr outside |
| `brake_temp_fr` | °C | Brake disc temperature FR |  | brake temp fr, fr brake temp, brake temp rf |
| `wheel_speed_fr` | km/h | Wheel speed FR |  | wheel speed fr, fr wheel speed, wheel speed rf |
| `tyre_press_fr` | bar | Tyre pressure FR |  | tyre press fr, fr tyre press, tyre press rf |
| `susp_travel_fr` | mm | Suspension travel FR | +ve in compression | susp travel fr, fr susp travel, susp travel rf |
| `tyre_temp_rl` | °C | Tyre temperature RL, single sensor (surface or core air) |  | tyre temp rl, rl tyre temp, tyre temp lr |
| `tyre_temp_rl_inner` | °C | Tyre surface temperature RL, inner zone |  | tyre temp rl inner, rl tyre temp inner, tyre temp rl inside |
| `tyre_temp_rl_mid` | °C | Tyre surface temperature RL, mid zone |  | tyre temp rl mid, rl tyre temp mid, tyre temp rl middle |
| `tyre_temp_rl_outer` | °C | Tyre surface temperature RL, outer zone |  | tyre temp rl outer, rl tyre temp outer, tyre temp rl outside |
| `brake_temp_rl` | °C | Brake disc temperature RL |  | brake temp rl, rl brake temp, brake temp lr |
| `wheel_speed_rl` | km/h | Wheel speed RL |  | wheel speed rl, rl wheel speed, wheel speed lr |
| `tyre_press_rl` | bar | Tyre pressure RL |  | tyre press rl, rl tyre press, tyre press lr |
| `susp_travel_rl` | mm | Suspension travel RL | +ve in compression | susp travel rl, rl susp travel, susp travel lr |
| `tyre_temp_rr` | °C | Tyre temperature RR, single sensor (surface or core air) |  | tyre temp rr, rr tyre temp, tyre temp rear right |
| `tyre_temp_rr_inner` | °C | Tyre surface temperature RR, inner zone |  | tyre temp rr inner, rr tyre temp inner, tyre temp rr inside |
| `tyre_temp_rr_mid` | °C | Tyre surface temperature RR, mid zone |  | tyre temp rr mid, rr tyre temp mid, tyre temp rr middle |
| `tyre_temp_rr_outer` | °C | Tyre surface temperature RR, outer zone |  | tyre temp rr outer, rr tyre temp outer, tyre temp rr outside |
| `brake_temp_rr` | °C | Brake disc temperature RR |  | brake temp rr, rr brake temp, brake temp rear right |
| `wheel_speed_rr` | km/h | Wheel speed RR |  | wheel speed rr, rr wheel speed, wheel speed rear right |
| `tyre_press_rr` | bar | Tyre pressure RR |  | tyre press rr, rr tyre press, tyre press rear right |
| `susp_travel_rr` | mm | Suspension travel RR | +ve in compression | susp travel rr, rr susp travel, susp travel rear right |

## Derived channels

| Channel | Unit | Description | Sign | Computed from |
|---|---|---|---|---|
| `lap_distance` | m | Distance from the start/finish line, scaled to the common lap length |  | time |
| `acc_long_calc` | g | Longitudinal acceleration from d(speed)/dt, 15 Hz low-pass (2 Hz when speed is recorded in coarse steps) | +ve accelerating | speed |
| `acc_lat_rebuilt` | g | Lateral acceleration rebuilt from damper roll, calibrated against the logged channel. Only present when acc_lat is recorded too coarsely to use; 5 Hz bandwidth | +ve in left-hand corners | acc_lat, susp_travel_fl, susp_travel_fr, susp_travel_rl, susp_travel_rr |
| `acc_lat_calc` | g | Lateral acceleration a neutral-steer car would make for the steering applied: V² δ / (g L) | +ve in left-hand corners | speed, steering |
| `steering_rate` | deg/s | d(steering)/dt, 10 Hz low-pass |  | steering |
| `yaw_rate_est` | rad/s | Steady-state yaw rate estimate: acc_lat · g / V | +ve turning left | speed, acc_lat |
| `oversteer_metric` | g | \|acc_lat\| − \|acc_lat_calc\| while cornering (>0.3 g) | +ve oversteer, -ve understeer | speed, steering, acc_lat |
| `llt_front` | N | Front axle lateral load transfer | +ve in left-hand corners | acc_lat |
| `llt_rear` | N | Rear axle lateral load transfer | +ve in left-hand corners | acc_lat |
| `lltd_front` | - | Front share of lateral load transfer, above 0.5 g (needs damper travel to carry information) |  | acc_lat |
| `grip_util` | - | Combined g as a fraction of the reference envelope |  | speed, acc_lat |
