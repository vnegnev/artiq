"""RTIO driver for the Fastino 32channel, 16 bit, 2.5 MS/s per channel,
streaming DAC.
"""

from artiq.language.core import kernel, portable, delay, delay_mu
from artiq.coredevice.rtio import (rtio_output, rtio_output_wide,
                                   rtio_input_data)
from artiq.language.units import us
from artiq.language.types import TInt32, TList


class Fastino:
    """Fastino 32-channel, 16-bit, 2.5 MS/s per channel streaming DAC

    The RTIO PHY supports staging DAC data before transmitting them by writing
    to the DAC RTIO addresses, if a channel is not "held" by setting its bit
    using :meth:`set_hold`, the next frame will contain the update. For the
    DACs held, the update is triggered explicitly by setting the corresponding
    bit using :meth:`set_update`. Update is self-clearing. This enables atomic
    DAC updates synchronized to a frame edge.

    The `log2_width=0` RTIO layout uses one DAC channel per RTIO address and a
    dense RTIO address space. The RTIO words are narrow. (32 bit) and
    few-channel updates are efficient. There is the least amount of DAC state
    tracking in kernels, at the cost of more DMA and RTIO data.
    The setting here and in the RTIO PHY (gateware) must match.

    Other `log2_width` (up to `log2_width=5`) settings pack multiple
    (in powers of two) DAC channels into one group and into one RTIO write.
    The RTIO data width increases accordingly. The `log2_width`
    LSBs of the RTIO address for a DAC channel write must be zero and the
    address space is sparse. For `log2_width=5` the RTIO data is 512 bit wide.

    If `log2_width` is zero, the :meth:`set_dac`/:meth:`set_dac_mu` interface
    must be used. If non-zero, the :meth:`set_group`/:meth:`set_group_mu`
    interface must be used.

    :param channel: RTIO channel number
    :param core_device: Core device name (default: "core")
    :param log2_width: Width of DAC channel group (logarithm base 2).
        Value must match the corresponding value in the RTIO PHY (gateware).
    :param order: CIC filter interpolation order.
    """
    kernel_invariants = {"core", "channel", "width", "order"}

    def __init__(self, dmgr, channel, core_device="core", log2_width=0, order=3):
        self.channel = channel << 8
        self.core = dmgr.get(core_device)
        self.width = 1 << log2_width
        self.order = order

    @kernel
    def init(self):
        """Initialize the device.

        This clears reset, unsets DAC_CLR, enables AFE_PWR,
        clears error counters, then enables error counting
        """
        self.set_cfg(reset=0, afe_power_down=0, dac_clr=0, clr_err=1)
        delay(1*us)
        self.set_cfg(reset=0, afe_power_down=0, dac_clr=0, clr_err=0)
        delay(1*us)

    @kernel
    def write(self, addr, data):
        """Write data to a Fastino register.

        :param addr: Address to write to.
        :param data: Data to write.
        """
        rtio_output(self.channel | addr, data)

    @kernel
    def read(self, addr):
        """Read from Fastino register.

        TODO: untested

        :param addr: Address to read from.
        :return: The data read.
        """
        rtio_output(self.channel | addr | 0x80)
        return rtio_input_data(self.channel >> 8)

    @kernel
    def set_dac_mu(self, dac, data):
        """Write DAC data in machine units.

        :param dac: DAC channel to write to (0-31).
        :param data: DAC word to write, 16 bit unsigned integer, in machine
            units.
        """
        self.write(dac, data)

    @kernel
    def set_group_mu(self, dac: TInt32, data: TList(TInt32)):
        """Write a group of DAC channels in machine units.

        :param dac: First channel in DAC channel group (0-31). The `log2_width`
            LSBs must be zero.
        :param data: List of DAC data pairs (2x16 bit unsigned) to write,
            in machine units. Data exceeding group size is ignored.
            If the list length is less than group size, the remaining
            DAC channels within the group are cleared to 0 (machine units).
        """
        if dac & (self.width - 1):
            raise ValueError("Group index LSBs must be zero")
        rtio_output_wide(self.channel | dac, data)

    @portable
    def voltage_to_mu(self, voltage):
        """Convert SI Volts to DAC machine units.

        :param voltage: Voltage in SI Volts.
        :return: DAC data word in machine units, 16 bit integer.
        """
        data = int(round((0x8000/10.)*voltage)) + 0x8000
        if data < 0 or data > 0xffff:
            raise ValueError("DAC voltage out of bounds")
        return data

    @portable
    def voltage_group_to_mu(self, voltage, data):
        """Convert SI Volts to packed DAC channel group machine units.

        :param voltage: List of SI Volt voltages.
        :param data: List of DAC channel data pairs to write to.
            Half the length of `voltage`.
        """
        for i in range(len(voltage)):
            v = self.voltage_to_mu(voltage[i])
            if i & 1:
                v = data[i // 2] | (v << 16)
            data[i // 2] = v

    @kernel
    def set_dac(self, dac, voltage):
        """Set DAC data to given voltage.

        :param dac: DAC channel (0-31).
        :param voltage: Desired output voltage.
        """
        self.write(dac, self.voltage_to_mu(voltage))

    @kernel
    def set_group(self, dac, voltage):
        """Set DAC group data to given voltage.

        :param dac: DAC channel (0-31).
        :param voltage: Desired output voltage.
        """
        data = [int32(0)] * (len(voltage) // 2)
        self.voltage_group_to_mu(voltage, data)
        self.set_group_mu(dac, data)

    @kernel
    def update(self, update):
        """Schedule channels for update.

        :param update: Bit mask of channels to update (32 bit).
        """
        self.write(0x20, update)

    @kernel
    def set_hold(self, hold):
        """Set channels to manual update.

        :param hold: Bit mask of channels to hold (32 bit).
        """
        self.write(0x21, hold)

    @kernel
    def set_cfg(self, reset=0, afe_power_down=0, dac_clr=0, clr_err=0):
        """Set configuration bits.

        :param reset: Reset SPI PLL and SPI clock domain.
        :param afe_power_down: Disable AFE power.
        :param dac_clr: Assert all 32 DAC_CLR signals setting all DACs to
            mid-scale (0 V).
        :param clr_err: Clear error counters and PLL reset indicator.
            This clears the sticky red error LED. Must be cleared to enable
            error counting.
        """
        self.write(0x22, (reset << 0) | (afe_power_down << 1) |
                   (dac_clr << 2) | (clr_err << 3))

    @kernel
    def set_leds(self, leds):
        """Set the green user-defined LEDs

        :param leds: LED status, 8 bit integer each bit corresponding to one
            green LED.
        """
        self.write(0x23, leds)

    @kernel
    def set_continuous(self, channel_mask):
        """Enable continuous DAC updates on channels regardless of new data
        being submitted.
        """
        self.write(0x25, channel_mask)

    @kernel
    def stage_cic_mu(self, rate_mantissa, rate_exponent, gain_exponent):
        """Stage machine unit interpolator configuration.
        """
        if rate_mantissa < 0 or rate_mantissa >= 1 << 6:
            raise ValueError("rate_mantissa out of bounds")
        if rate_exponent < 0 or rate_exponent >= 1 << 4:
            raise ValueError("rate_exponent out of bounds")
        if gain_exponent < 0 or gain_exponent >= 1 << 6:
            raise ValueError("gain_exponent out of bounds")
        config = rate_mantissa | (rate_exponent << 6) | (gain_exponent << 10)
        self.write(0x26, config)

    @kernel
    def stage_cic(self, rate) -> TInt32:
        """Compute and stage interpolator configuration.

        Approximates rate using 6+4 bit floating point representation,
        approximates optimal interpolation gain compensation exponent to avoid
        clipping. Gains for rates that are powers of two are accurately
        compensated. Other rates lead to overall less than unity gain.

        Returns the actual interpolation rate.
        The actual overall interpolation gain including gain compensation is
        `actual_rate**order/2**ceil(log2(actual_rate**order))`.
        """
        if rate <= 0 or rate > 1 << 16:
            raise ValueError("rate out of bounds")
        rate_mantissa = rate
        rate_exponent = 0
        while rate_mantissa > 1 << 6:
            rate_exponent += 1
            rate_mantissa >>= 1
        gain = 1
        for i in range(self.order):
            gain *= rate_mantissa
        gain_exponent = 0
        while gain > 1 << gain_exponent:
            gain_exponent += 1
        gain_exponent += self.order*rate_exponent
        assert gain_exponent <= self.order*16
        self.stage_cic_mu(rate_mantissa - 1, rate_exponent, gain_exponent)
        return rate_mantissa << rate_exponent

    @kernel
    def apply_cic(self, channel_mask):
        """Apply the staged interpolator configuration on the specified channels.

        Channels using non-unity interpolation rate and variable data should have
        continous DAC updates enabled (see :meth:`set_continuous`).

        This resets and settles the interpolators. There will be no output
        updates for the next `order` input samples.
        """
        self.write(0x27, channel_mask)
