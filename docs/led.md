# The front LED

The unit's front status LED says what the receiver is doing, so that setting it up and using it
needs no screen. It is four emitters behind one lens (red, green, blue, white), each dimmed
separately, driven by `w1700k-led` on the unit from the moment it has booted.

## Legend

| What you see | What it means |
| --- | --- |
| White for a moment, then steady red | The loader and the kernel, before our software runs. Steady red that never changes: the unit did not finish booting, or `w1700k-led` is not running. |
| The colours of the rainbow once round, brightening out of the dark (3 s) | Starting: the unit has booted and the LED is ours from here on. |
| **Yellow, breathing slowly** | No link on the stream port: no cable, or nothing at the far end. |
| **White, low and steady** | Powered and reachable, the stream port linked; the radio is not loaded. |
| **Green, steady** | Ready: the radio is loaded and tuned, and not streaming. |
| **Cyan**, **blue**, **violet** or **magenta** | Streaming at 40, 80, 160 or 320 Msps. |
| … that colour **solid** | Every sample is carried bit for bit (raw or packed, lossless). |
| … that colour **breathing** | The samples are reduced to fewer bits to fit the link. |
| … that colour **blinking slowly**, lit as long as dark | A stream was started but nothing is leaving the port. |
| **Yellow, blinking** | Look: the radio is loaded, but the synthesizer of the band being captured is out of lock. |
| **Red, blinking** | Stop: the stream ended by itself. The host's next request clears it. |
| A short lift towards white | The host has just changed something: the unit heard it. |

Green, yellow and red mean go, look and stop wherever they appear; the colours of a stream keep
clear of them. A change from one state to another fades over about half a second.

## How it decides

Every quarter of a second `w1700k-led` looks at what the unit knows of itself:

- the stream port's carrier, and whether bytes are leaving it (`/sys/class/net/eth1`);
- the controller's state record, which `w1700k` writes to the unit whenever it changes something
  (`/tmp/w1700k-state.json`): whether the radio is loaded, the band, its sample rate, the
  stream's packing, and the synthesizers' lock;
- the mark a stream's pump leaves when it ends (`/tmp/w1700k-stream.done`) while that record
  still says a stream is running.

`w1700k-led /dev/w1700k-sdr --status` prints that and the look that goes with it;
`--demo` walks through every look, and `--show H S B` sets one colour.

## How it shows it

Colours are hue, saturation and brightness (`kernel/w1700k_sdr/ledd.h`). Every look is a colour
in time (steady, breathing, blinking, or the hue going round), and every fade and mix is done on
those three numbers: the hue goes the shorter way round, and white and dark, which have no hue of
their own, take the hue of what they fade to or from, so a fade never passes through a colour
neither end has. Only the last step turns a colour into the four emitters' levels: the hue's mix
of red, green and blue, what the three share handed to the white emitter (white is the white
emitter alone), and all of it scaled by the brightness, which is perceptual (the light is the
brightness to the power 2.2).

The levels go to the driver (`W1700K_SDR_LED`, four values 0 to 255), which has the SoC's own
waveform generators dim the four pins at 250 Hz (`kernel/w1700k_sdr/led.c`): nothing in software
toggles a pin, so the LED is steady however busy the unit is. Rendering fifty frames a second
costs under 1 % of one core.

| Stream | Hue |
| --- | --- |
| 40 Msps | 185° (cyan) |
| 80 Msps | 225° (blue) |
| 160 Msps | 265° (violet) |
| 320 Msps (and 640/2) | 300° (magenta) |
