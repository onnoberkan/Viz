from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Calibration:
    microvolts_per_count: float | None
    detail: str


def voltage_calibration(channel):
    unknown = Calibration(None, 'Voltage calibration unavailable. Values are native ADC samples, not microvolts.')
    if channel is None or not hasattr(channel, 'key'): return unknown
    sources = [p.source for p in channel.parts] + [s for s, _ in channel.pending]
    factors = []
    seen = set()
    for source in sources:
        if source.path in seen: continue
        seen.add(source.path)
        definition = source.definitions.get(channel.key[1], {})
        version = definition.get('format_version')
        if version == 3:
            return Calibration(None, 'Legacy MAP: electrode voltage calibration is unavailable. '
                'The amplitude field is not the MPX ADC resolution. Values are native ADC samples; '
                'the acquisition calibration is required to convert them to microvolts.')
        if version != 4: return unknown
        try: resolution, gain = float(definition['amp']), float(definition['gain']) / 100
        except (KeyError, TypeError, ValueError): return unknown
        if not math.isfinite(resolution) or resolution < 0 or not math.isfinite(gain) or gain <= 0:
            return Calibration(None, 'Missing or invalid ADC resolution / total gain. '
                'Values are native ADC samples; voltage calibration is unavailable.')
        if resolution <= 5: resolution = 1_250_000 / 2**15
        factors.append(resolution / gain)
    if not factors or any(not math.isclose(value, factors[0], rel_tol=1e-9) for value in factors):
        return unknown
    factor = factors[0]
    return Calibration(factor, f'Input voltage: {factor:.9g} µV per ADC sample '
                       '(ADC resolution ÷ total channel gain).')


def set_voltage_axis(plot, channel, *, filtered=False):
    calibration = voltage_calibration(channel)
    axis = plot.getAxis('left')
    axis.enableAutoSIPrefix(False)
    factor = calibration.microvolts_per_count
    axis.setScale(factor if factor is not None else 1.0)
    if factor is None:
        plot.setLabel('left', 'Uncalibrated', units='')
    else:
        plot.setLabel('left', 'Filtered voltage' if filtered else 'Voltage', units='µV')
    axis.setToolTip(calibration.detail)
    return calibration
