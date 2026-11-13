"""Decode raw Renogy DC-DC charger register values for display.

charger_telemetry stores the raw register values the gateway read (see the
table comment in bms_schema.sql). The meanings below come from Renogy's
"DCC Charger Controller Modbus Protocol V1.0", transcribed in
~/Code/Renogy/dcc-register-map.md. Codes outside these tables are reported
as unknown rather than guessed.
"""

from typing import Dict, List, Optional

# 0x0120, low byte.
CHARGING_STATES = {
    0: 'Not charging',
    # The spec says "MPPT charging mode, solar", but the alternator-only
    # RBC40D1U reports it briefly while ramping its charge rate up or down,
    # so it reads as power-point tracking, not a solar source.
    2: 'MPPT tracking',
    3: 'Equalization',
    4: 'Boost',
    5: 'Float',
    6: 'Current limiting',
    8: 'DC (alternator)',
}

# 0x0125.
MONITOR_MODES = {
    0: 'Standby',
    1: 'Alternator charging house battery',
    2: 'House battery reverse-charging starter',
    3: 'Solar charging house battery',
    4: 'Solar and alternator charging house battery',
    5: 'Solar reverse-charging starter',
}

# 0x0121 (high word) and 0x0122 (low word), bit number -> meaning.
FAULTS_HI_BITS = {
    11: 'House battery too cold to charge',
    10: 'BMS overcharge protection',
    9: 'Starter battery reverse polarity',
    8: 'Alternator overvoltage',
    5: 'Alternator charging overcurrent',
    4: 'Controller overheating (2)',
    2: 'Starter battery current limiting',
    1: 'Starter battery low voltage',
}
FAULTS_LO_BITS = {
    12: 'Solar panel reverse polarity',
    11: 'Load short circuit',
    10: 'Load overcurrent',
    9: 'PV input overvoltage',
    8: 'Fan alarm',
    7: 'PV input power too high',
    6: 'House battery overheating',
    5: 'Controller overheating (1)',
    3: 'House battery reverse polarity',
    2: 'House battery undervoltage warning',
    1: 'House battery overvoltage',
    0: 'House battery over-discharge',
}


def _decode_fault_word(word: Optional[int], bits: Dict[int, str], register: str) -> List[str]:
    if not word:
        return []
    names = []
    for bit in range(16):
        if word & (1 << bit):
            names.append(bits.get(bit, f'Unknown fault ({register} bit {bit})'))
    return names


def decode_charger_reading(reading: Optional[Dict]) -> Optional[Dict]:
    """Add human-readable labels to a charger row, in place, and return it."""
    if not reading:
        return reading
    state = reading.get('charging_state')
    mode = reading.get('monitor_mode')
    reading['charging_state_label'] = (
        CHARGING_STATES.get(state, f'Unknown ({state})') if state is not None else None
    )
    reading['monitor_mode_label'] = (
        MONITOR_MODES.get(mode, f'Unknown ({mode})') if mode is not None else None
    )
    reading['faults'] = (
        _decode_fault_word(reading.get('faults_hi'), FAULTS_HI_BITS, '0x0121')
        + _decode_fault_word(reading.get('faults_lo'), FAULTS_LO_BITS, '0x0122')
    )
    return reading
