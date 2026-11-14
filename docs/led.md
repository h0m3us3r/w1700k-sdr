# The front LED

The unit's front status LED says what the receiver is doing, so that setting it up and using it
needs no screen. It is four emitters behind one lens (red, green, blue, white), each dimmed
separately, driven by `w1700k-led` on the unit from the moment it has booted.

## Legend

| | What you see | What it means |
| --- | --- | --- |
| <img src="images/led/loader.svg" width="48" height="20" alt="white, then red"> | White for a moment, then steady red | The loader and the kernel, before our software runs. Steady red that never changes: the unit did not finish booting, or `w1700k-led` is not running. |
| <img src="images/led/start.svg" width="48" height="20" alt="every colour once, out of the dark"> | The colours of the rainbow once round, brightening out of the dark (3 s) | Starting: the unit has booted and the LED is ours from here on. |
| <img src="images/led/no-link.svg" width="48" height="20" alt="yellow, breathing"> | **Yellow, breathing slowly** | No link on the stream port: no cable, or nothing at the far end. Also while the two ends agree on a speed after the unit starts: about 6 s, or about 18 s where the far end offers 10 Gbit/s and cannot train at it. |
| <img src="images/led/idle.svg" width="48" height="20" alt="white, brightening"> | **White, brightening** from low to full over six seconds, then steady | Powered and reachable, the stream port linked; the radio is not loaded, or is being loaded. |
| <img src="images/led/ready.svg" width="48" height="20" alt="green"> | **Green, steady** | Ready: the radio is loaded and tuned, and not streaming. |
| <img src="images/led/rates.svg" width="48" height="20" alt="cyan, blue, magenta"> | **Cyan**, **blue** or **magenta** | Streaming at 40, 80 or 160 Msps. |
| <img src="images/led/rainbow.svg" width="48" height="20" alt="every colour in turn"> | **The rainbow**, every colour in turn, once round in six seconds | Streaming at 320 Msps: everything the receiver has. |
| <img src="images/led/steady.svg" width="48" height="20" alt="steady"> | … at a **steady** brightness | Every sample is carried bit for bit (raw or packed, lossless). |
| <img src="images/led/breathing.svg" width="48" height="20" alt="breathing"> | … **breathing** | The samples are reduced to fewer bits to fit the link. |
| <img src="images/led/stalled.svg" width="48" height="20" alt="blinking slowly"> | … **blinking slowly**, lit as long as dark | A stream was started but nothing is leaving the port. |
| <img src="images/led/warning.svg" width="48" height="20" alt="yellow, blinking"> | **Yellow, blinking** | Look: the radio is loaded, but the synthesizer of the band being captured is out of lock. |
| <img src="images/led/fault.svg" width="48" height="20" alt="red, blinking"> | **Red, blinking** | Stop: the stream ended by itself. The host's next request clears it. |
| <img src="images/led/heard.svg" width="48" height="20" alt="a short lift towards white"> | A short lift towards white | The host has just changed something: the unit heard it. One lift for one operation, however many steps it takes. |

The swatches move as the LED does: they breathe, blink and turn at its pace (the one for a stream's
brightness is drawn in cyan, and stands for any of the stream colours). Green, yellow and red mean go, look and stop wherever they stand still or blink on their own. The
fixed colours of a stream keep clear of them (a third of the circle from each other, and as far
from green and red); the rainbow only passes through them, always moving. A change from one
state to another fades over about half a second.

The white fills like a bar while the unit waits for its radio. Whatever comes next takes over at
once, without waiting for it to fill; if the unit comes back to white before it had filled, it
goes on from where it was, and once it has filled, the next time starts from low again.

## The reset button

The button behind the paperclip hole changes the unit's one slot for a login key
([init/README.md](../init/README.md#the-paperclip-key)). While it is held, and for a moment after,
the LED is the button's: it shows what letting go would do, and then what was done. The receiver's
look returns when that is over.

| | What you see | What it means |
| --- | --- | --- |
| Held, first 5 s | **Green, rising** out of the dark | Letting go at 5 s will switch the slot's key **on** |
| | **Red, sinking** from full | Letting go at 5 s will switch the slot's key **off** |
| | **White, rising** | The slot is empty: nothing to switch |
| Held 5 s | A white flash, then the same colour **pulsing** | Let go now and it is switched |
| Held 10 s | A white flash, then **blue pulsing**, faster | Let go now and the slot opens for a new key |
| Held 15 s | A white flash, then **red blinking hard**, lit and dark | Let go now and the slot's key is erased |
| Let go early | What had filled runs out | Nothing happened |
| After letting go | **Green, twice up** and staying a moment | Switched on |
| | **Red, sinking** into the dark | Switched off |
| | **Blue, breathing** | Waiting for a key (`init/key`), up to three minutes; a press ends it |
| | A flash, then **blue turning through cyan to green** | The key is in |
| | The blue goes out | The wait ended without a key |
| | The red pales to white as it sinks | Erased |
| | **Red, three times** | The key could not be kept |

Which way the key will be switched is said twice, by colour and by direction (rising or sinking),
because red against green is the colour blindness most people with one have. Green and red keep
their meaning here (go, stop); blue is the colour of waiting to be paired. `w1700k-led DEVICE
--demo` walks through these after the receiver's looks.

## How it decides

Every quarter of a second `w1700k-led` looks at what the unit knows of itself:

- the stream port's carrier, and whether bytes are leaving it (`/sys/class/net/eth1`);
- the controller's state record, which `w1700k` writes to the unit whenever it changes something
  (`/tmp/w1700k-state.json`): whether the radio is loaded, the band, its sample rate, the
  stream's packing, and the synthesizers' lock;
- the mark a stream's pump leaves when it ends (`/tmp/w1700k-stream.done`) while that record
  still says a stream is running.

- what the reset button is doing, which `w1700k-key` writes down the moment it changes
  (`/run/w1700k-key`): this one is looked at every frame, and read when it has changed.

`w1700k-led /dev/w1700k-sdr --status` prints the first three and the look that goes with it;
`--demo` walks through every look, and `--show H S B` sets one colour.

## How it shows it

Colours are hue, saturation and brightness (`kernel/w1700k_sdr/ledd.h`). Every look is a colour
in time (a brightness that is steady, breathing or blinking, and a hue that stays or goes round), and every fade and mix is done on
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
| 40 Msps | 180° (cyan) |
| 80 Msps | 240° (blue) |
| 160 Msps | 300° (magenta) |
| 320 Msps (and 640/2) | all of them: once round the circle in 6 s |

The rainbow's turn was chosen on a unit from 4, 6 and 8 seconds. When it breathes (a reduced
320 Msps stream), a breath takes 2.5 s rather than the fixed colours' 3: half the turn would dim
the same two colours every time, and at 2.5 s each colour has its turn at full brightness.
