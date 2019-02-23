# The MIT License (MIT)
#
# Copyright (c) 2019 Limor Fried for Adafruit Industries
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""
`adafruit_pyportal`
================================================================================

CircuitPython driver for Adafruit PyPortal.


* Author(s): Limor Fried

Implementation Notes
--------------------

**Hardware:**

* `Adafruit PyPortal <https://www.adafruit.com/product/4116>`_

**Software and Dependencies:**

* Adafruit CircuitPython firmware for the supported boards:
  https://github.com/adafruit/circuitpython/releases

* Adafruit's Bus Device library: https://github.com/adafruit/Adafruit_CircuitPython_BusDevice
"""

import os
import time
import gc
import board
import busio
import microcontroller
from digitalio import DigitalInOut
import adafruit_touchscreen
import pulseio
import neopixel

from adafruit_esp32spi import adafruit_esp32spi
import adafruit_esp32spi.adafruit_esp32spi_requests as requests
from adafruit_display_text.text_area import TextArea
from adafruit_bitmap_font import bitmap_font

import displayio
import audioio
import rtc
import supervisor

try:
    from settings import settings
except ImportError:
    print("""WiFi settings are kept in settings.py, please add them there!
the settings dictionary must contain 'ssid' and 'password' at a minimum""")
    raise

__version__ = "0.0.0-auto.0"
__repo__ = "https://github.com/adafruit/Adafruit_CircuitPython_PyPortal.git"

# pylint: disable=line-too-long
IMAGE_CONVERTER_SERVICE = "http://res.cloudinary.com/schmarty/image/fetch/w_320,h_240,c_fill,f_bmp/"
#IMAGE_CONVERTER_SERVICE = "http://ec2-107-23-37-170.compute-1.amazonaws.com/rx/ofmt_bmp,rz_320x240/"
TIME_SERVICE_IPADDR = "http://worldtimeapi.org/api/ip"
TIME_SERVICE_LOCATION = "http://worldtimeapi.org/api/timezone/"
LOCALFILE = "local.txt"
# pylint: enable=line-too-long


class Fake_Requests:
    """For requests using a local file instead of the network."""
    def __init__(self, filename):
        self._filename = filename
        with open(filename, "r") as file:
            self.text = file.read()

    def json(self):
        """json for local requests."""
        import json
        return json.loads(self.text)


class PyPortal:
    """Class representing the Adafruit PyPortal.

    :param url: The URL of your data source. Defaults to ``None``.
    :param json_path: Defaults to ``None``.
    :param regexp_path: Defaults to ``None``.
    :param default_bg: The path to your default background file. Defaults to ``None``.
    :param status_neopixel: The pin for the status NeoPixel. Use ``board.NeoPixel`` for the
                            on-board NeoPixel. Defaults to ``None``.
    :param str text_font: The path to your font file for your text.
    :param text_position: The position of your text on the display.
    :param text_color: The color of the text. Defaults to ``None``.
    :param text_wrap: The location where the text wraps. Defaults to ``None``.
    :param text_maxlen: The max length of the text. Defaults to ``None``.
    :param image_json_path: Defaults to ``None``.
    :param image_resize: Defaults to ``None``.
    :param image_position: The position of the image on the display. Defaults to ``None``.
    :param time_between_requests: Defaults to ``None``.
    :param success_callback: Defaults to ``None``.
    :param str caption_text: The text of your caption. Defaults to ``None``.
    :param str caption_font: The path to the font file for your caption. Defaults to ``None``.
    :param caption_position: The position of your caption on the display. Defaults to ``None``.
    :param caption_color: The color of your caption. Must be a hex value, e.g. ``0x808000``.
    :param debug: Turn on debug features. Defaults to False.

    """
    # pylint: disable=too-many-instance-attributes, too-many-locals, too-many-branches, too-many-statements
    def __init__(self, *, url=None, json_path=None, regexp_path=None,
                 default_bg=None, status_neopixel=None,
                 text_font=None, text_position=None, text_color=0x808080,
                 text_wrap=0, text_maxlen=0,
                 image_json_path=None, image_resize=None, image_position=None,
                 time_between_requests=60, success_callback=None,
                 caption_text=None, caption_font=None, caption_position=None,
                 caption_color=0x808080,
                 debug=False):

        self._debug = debug

        try:
            self._backlight = pulseio.PWMOut(board.TFT_BACKLIGHT)  # pylint: disable=no-member
        except ValueError:
            self._backlight = None
        self.set_backlight(1.0)  # turn on backlight

        self._url = url
        if json_path:
            if isinstance(json_path[0], (list, tuple)):
                self._json_path = json_path
            else:
                self._json_path = (json_path,)
        else:
            self._json_path = None

        self._regexp_path = regexp_path
        self._time_between_requests = time_between_requests
        self._success_callback = success_callback

        if status_neopixel:
            self.neopix = neopixel.NeoPixel(status_neopixel, 1, brightness=0.2)
        else:
            self.neopix = None
        self.neo_status(0)

        try:
            os.stat(LOCALFILE)
            self._uselocal = True
        except OSError:
            self._uselocal = False

        # Make ESP32 connection
        if self._debug:
            print("Init ESP32")
        # pylint: disable=no-member
        esp32_cs = DigitalInOut(microcontroller.pin.PB14) # PB14
        esp32_ready = DigitalInOut(microcontroller.pin.PB16)
        esp32_gpio0 = DigitalInOut(microcontroller.pin.PB15)
        esp32_reset = DigitalInOut(microcontroller.pin.PB17)
        spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
        # pylint: enable=no-member

        if not self._uselocal:
            self._esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset,
                                                         esp32_gpio0)
            #self._esp._debug = 1
            for _ in range(3): # retries
                try:
                    print("ESP firmware:", self._esp.firmware_version)
                    break
                except RuntimeError:
                    print("Retrying ESP32 connection")
                    time.sleep(1)
                    self._esp.reset()
            else:
                raise RuntimeError("Was not able to find ESP32")

            requests.set_interface(self._esp)

        if self._debug:
            print("Init display")
        self.splash = displayio.Group(max_size=5)
        board.DISPLAY.show(self.splash)

        if self._debug:
            print("Init background")
        self._bg_group = displayio.Group(max_size=1)
        self._bg_file = None
        self._default_bg = default_bg
        self.set_background(self._default_bg)
        self.splash.append(self._bg_group)

        self._qr_group = None

        if self._debug:
            print("Init caption")
        self._caption = None
        if caption_font:
            self._caption_font = bitmap_font.load_font(caption_font)
        self.set_caption(caption_text, caption_position, caption_color)

        if text_font:
            if isinstance(text_position[0], (list, tuple)):
                num = len(text_position)
                if not text_wrap:
                    text_wrap = [0] * num
                if not text_maxlen:
                    text_maxlen = [0] * num
            else:
                num = 1
                text_position = (text_position,)
                text_color = (text_color,)
                text_wrap = (text_wrap,)
                text_maxlen = (text_maxlen,)
            self._text = [None] * num
            self._text_color = [None] * num
            self._text_position = [None] * num
            self._text_wrap = [None] * num
            self._text_maxlen = [None] * num
            self._text_font = bitmap_font.load_font(text_font)
            if self._debug:
                print("Loading font glyphs")
            # self._text_font.load_glyphs(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            #                             b'0123456789:/-_,. ')
            gc.collect()

            for i in range(num):
                if self._debug:
                    print("Init text area", i)
                self._text[i] = None
                self._text_color[i] = text_color[i]
                self._text_position[i] = text_position[i]
                self._text_wrap[i] = text_wrap[i]
                self._text_maxlen[i] = text_maxlen[i]
        else:
            self._text_font = None
            self._text = None

        self._image_json_path = image_json_path
        self._image_resize = image_resize
        self._image_position = image_position
        if image_json_path:
            if self._debug:
                print("Init image path")
            if not self._image_position:
                self._image_position = (0, 0)  # default to top corner
            if not self._image_resize:
                self._image_resize = (320, 240)  # default to full screen

        if self._debug:
            print("Init touchscreen")
        # pylint: disable=no-member
        self.touchscreen = adafruit_touchscreen.Touchscreen(microcontroller.pin.PB01,
                                                            microcontroller.pin.PB08,
                                                            microcontroller.pin.PA06,
                                                            microcontroller.pin.PB00,
                                                            calibration=((5200, 59000),
                                                                         (5800, 57000)),
                                                            size=(320, 240))
        # pylint: enable=no-member

        self.set_backlight(1.0)  # turn on backlight
        gc.collect()

    def set_background(self, filename):
        """The background image.

        :param filename: The name of the chosen background image file.

        """
        print("Set background to ", filename)
        try:
            self._bg_group.pop()
        except IndexError:
            pass  # s'ok, we'll fix to test once we can

        if not filename:
            return  # we're done, no background desired
        if self._bg_file:
            self._bg_file.close()
        self._bg_file = open(filename, "rb")
        background = displayio.OnDiskBitmap(self._bg_file)
        try:
            self._bg_sprite = displayio.TileGrid(background,
                                                 pixel_shader=displayio.ColorConverter(),
                                                 position=(0, 0))
        except AttributeError:
            self._bg_sprite = displayio.Sprite(background, pixel_shader=displayio.ColorConverter(),
                                               position=(0, 0))

        self._bg_group.append(self._bg_sprite)
        board.DISPLAY.refresh_soon()
        gc.collect()
        board.DISPLAY.wait_for_frame()

    def set_backlight(self, val):
        """The backlight.

        :param val: The backlight brightness. Use a value between ``0`` and ``1``, where ``0`` is
                    off, and ``1`` is 100% brightness.

        """
        val = max(0, min(1.0, val))
        if self._backlight:
            self._backlight.duty_cycle = int(val * 65535)
        else:
            board.DISPLAY.auto_brightness = False
            board.DISPLAY.brightness = val

    def preload_font(self, glyphs=None):
        """Preload font.

        :param glyphs: The font glyphs to load. Defaults to ``None``, uses built in glyphs if None.

        """
        if not glyphs:
            glyphs = b'0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-!,. "\'?!'
        print("Preloading font glyphs:", glyphs)
        if self._text_font:
            self._text_font.load_glyphs(glyphs)

    def set_caption(self, caption_text, caption_position, caption_color):
        """A caption. Requires setting ``caption_font`` in init!

        :param caption_text: The text of the caption.
        :param caption_position: The position of the caption text.
        :param caption_color: The color of your caption text. Must be a hex value,
                              e.g. ``0x808000``.

        """
        if self._debug:
            print("Setting caption to", caption_text)

        if (not caption_text) or (not self._caption_font) or (not caption_position):
            return  # nothing to do!

        if self._caption:
            self._caption._update_text(str(caption_text))  # pylint: disable=protected-access, undefined-variable
            board.DISPLAY.refresh_soon()
            board.DISPLAY.wait_for_frame()
            return

        self._caption = TextArea(self._caption_font, text=str(caption_text))
        self._caption.x = caption_position[0]
        self._caption.y = caption_position[1]
        self._caption.color = caption_color
        self.splash.append(self._caption)

    def set_text(self, val, index=0):
        """Display text.

        :param str val: The text to be displayed.
        :param index: Defaults to 0.

        """
        if self._text_font:
            string = str(val)
            if self._text_maxlen[index]:
                string = string[:self._text_maxlen[index]]
            if self._text[index]:
                # print("Replacing text area with :", string)
                # self._text[index].text = string
                # return
                items = []
                while len(self.splash):  # pylint: disable=len-as-condition
                    item = self.splash.pop()
                    if item == self._text[index]:
                        break
                    items.append(item)
                self._text[index] = TextArea(self._text_font, text=string)
                self._text[index].color = self._text_color[index]
                self._text[index].x = self._text_position[index][0]
                self._text[index].y = self._text_position[index][1]
                self.splash.append(self._text[index])
                for g in items:
                    self.splash.append(g)
                return

            if self._text_position[index]:  # if we want it placed somewhere...
                print("Making text area with string:", string)
                self._text[index] = TextArea(self._text_font, text=string)
                self._text[index].color = self._text_color[index]
                self._text[index].x = self._text_position[index][0]
                self._text[index].y = self._text_position[index][1]
                self.splash.append(self._text[index])

    def neo_status(self, value):
        """The status NeoPixeel.

        :param value: The color to change the NeoPixel.

        """
        if self.neopix:
            self.neopix.fill(value)

    @staticmethod
    def play_file(file_name):
        """Play a wav file.

        :param str file_name: The name of the wav file.

        """
        #self._speaker_enable.value = True
        with audioio.AudioOut(board.AUDIO_OUT) as audio:
            with open(file_name, "rb") as file:
                with audioio.WaveFile(file) as wavefile:
                    audio.play(wavefile)
                    while audio.playing:
                        pass
        #self._speaker_enable.value = False

    @staticmethod
    def _json_traverse(json, path):
        value = json
        for x in path:
            value = value[x]
            gc.collect()
        return value

    def get_local_time(self, location=None):
        """The local time.

        :param str location: Your city and state, e.g. ``"New York, New York"``.

        """
        self._connect_esp()
        api_url = None
        if not location:
            api_url = TIME_SERVICE_IPADDR
        else:
            api_url = TIME_SERVICE_LOCATION + location
        response = requests.get(api_url)
        time_json = response.json()
        current_time = time_json['datetime']
        year_day = time_json['day_of_year']
        week_day = time_json['day_of_week']
        is_dst = time_json['dst']

        the_date, the_time = current_time.split('T')
        year, month, mday = [int(x) for x in the_date.split('-')]
        the_time = the_time.split('.')[0]
        hours, minutes, seconds = [int(x) for x in the_time.split(':')]
        now = time.struct_time((year, month, mday, hours, minutes, seconds, week_day, year_day,
                                is_dst))
        print(now)
        rtc.RTC().datetime = now

        # now clean up
        time_json = None
        response.close()
        response = None
        gc.collect()

    def wget(self, url, filename):
        """Obtain a stream.

        :param url: The URL from which to obtain the data.
        :param filename: The name of the file to save the data.

        """
        print("Fetching stream from", url)

        self.neo_status((100, 100, 0))
        r = requests.get(url, stream=True)

        if self._debug:
            print(r.headers)
        content_length = int(r.headers['content-length'])
        remaining = content_length
        print("Saving data to ", filename)
        stamp = time.monotonic()
        with open(filename, "wb") as file:
            for i in r.iter_content(min(remaining, 12000)):  # huge chunks!
                self.neo_status((0, 100, 100))
                remaining -= len(i)
                file.write(i)
                if self._debug:
                    print("Read %d bytes, %d remaining" % (content_length-remaining, remaining))
                else:
                    print(".", end='')
                if not remaining:
                    break
                self.neo_status((100, 100, 0))

        r.close()
        stamp = time.monotonic() - stamp
        print("Created file of %d bytes in %0.1f seconds" % (os.stat(filename)[6], stamp))
        self.neo_status((0, 0, 0))

    def _connect_esp(self):
        self.neo_status((0, 0, 100))
        while not self._esp.is_connected:
            if self._debug:
                print("Connecting to AP")
            # settings dictionary must contain 'ssid' and 'password' at a minimum
            self.neo_status((100, 0, 0)) # red = not connected
            self._esp.connect(settings)

    def fetch(self):
        """Fetch data."""
        json_out = None
        image_url = None
        values = []

        gc.collect()
        if self._debug:
            print("Free mem: ", gc.mem_free())  # pylint: disable=no-member

        r = None
        if self._uselocal:
            print("*** USING LOCALFILE FOR DATA - NOT INTERNET!!! ***")
            r = Fake_Requests(LOCALFILE)

        if not r:
            self._connect_esp()
            # great, lets get the data
            print("Retrieving data...", end='')
            self.neo_status((100, 100, 0))   # yellow = fetching data
            gc.collect()
            r = requests.get(self._url)
            gc.collect()
            self.neo_status((0, 0, 100))   # green = got data
            print("Reply is OK!")

        if self._debug:
            print(r.text)

        if self._image_json_path or self._json_path:
            try:
                gc.collect()
                json_out = r.json()
                gc.collect()
            except ValueError:            # failed to parse?
                print("Couldn't parse json: ", r.text)
                raise
            except MemoryError:
                supervisor.reload()

        if self._regexp_path:
            import ure

        # extract desired text/values from json
        if self._json_path:
            for path in self._json_path:
                values.append(PyPortal._json_traverse(json_out, path))
        elif self._regexp_path:
            for regexp in self._regexp_path:
                values.append(ure.search(regexp, r.text).group(1))
        else:
            values = r.text

        if self._image_json_path:
            try:
                image_url = PyPortal._json_traverse(json_out, self._image_json_path)
            except KeyError as error:
                print("Error finding image data. '" + error.args[0] + "' not found.")
                self.set_background(self._default_bg)

        # we're done with the requests object, lets delete it so we can do more!
        json_out = None
        r = None
        gc.collect()

        if image_url:
            try:
                print("original URL:", image_url)
                image_url = IMAGE_CONVERTER_SERVICE+image_url
                print("convert URL:", image_url)
                # convert image to bitmap and cache
                #print("**not actually wgetting**")
                self.wget(image_url, "/cache.bmp")
                self.set_background("/cache.bmp")
            except ValueError as error:
                print("Error displaying cached image. " + error.args[0])
                self.set_background(self._default_bg)
            finally:
                image_url = None
                gc.collect()

        # if we have a callback registered, call it now
        if self._success_callback:
            self._success_callback(values)

        # fill out all the text blocks
        if self._text:
            for i in range(len(self._text)):
                string = None
                try:
                    string = "{:,d}".format(int(values[i]))
                except (TypeError, ValueError):
                    string = values[i] # ok its a string
                if self._debug:
                    print("Drawing text", string)
                if self._text_wrap[i]:
                    if self._debug:
                        print("Wrapping text")
                    string = '\n'.join(PyPortal.wrap_nicely(string, self._text_wrap[i]))
                self.set_text(string, index=i)
        if len(values) == 1:
            return values[0]
        return values

    def show_QR(self, qr_data, qr_size=128, position=None):  # pylint: disable=invalid-name
        """Display a QR code.

        :param qr_data: The data for the QR code.
        :param int qr_size: The size of the QR code in pixels.
        :param position: The position of the QR code on the display.

        """
        import adafruit_miniqr

        if not qr_data:  # delete it
            if self._qr_group:
                try:
                    self._qr_group.pop()
                except IndexError:
                    pass
                board.DISPLAY.refresh_soon()
                board.DISPLAY.wait_for_frame()
            return

        if not position:
            position = (0, 0)
        if qr_size % 32 != 0:
            raise RuntimeError("QR size must be divisible by 32")

        qrcode = adafruit_miniqr.QRCode()
        qrcode.add_data(qr_data)
        qrcode.make()

        # pylint: disable=invalid-name
        # how big each pixel is, add 2 blocks on either side
        BLOCK_SIZE = qr_size // (qrcode.matrix.width+4)
        # Center the QR code in the middle
        X_OFFSET = (qr_size - BLOCK_SIZE * qrcode.matrix.width) // 2
        Y_OFFSET = (qr_size - BLOCK_SIZE * qrcode.matrix.height) // 2

        # monochome (2 color) palette
        palette = displayio.Palette(2)
        palette[0] = 0xFFFFFF
        palette[1] = 0x000000

        # bitmap the size of the matrix + borders, monochrome (2 colors)
        qr_bitmap = displayio.Bitmap(qr_size, qr_size, 2)

        # raster the QR code
        line = bytearray(qr_size // 8)  # monochrome means 8 pixels per byte
        for y in range(qrcode.matrix.height):    # each scanline in the height
            for i, _ in enumerate(line):    # initialize it to be empty
                line[i] = 0
            for x in range(qrcode.matrix.width):
                if qrcode.matrix[x, y]:
                    for b in range(BLOCK_SIZE):
                        _x = X_OFFSET + x * BLOCK_SIZE + b
                        line[_x // 8] |= 1 << (7-(_x % 8))

            for b in range(BLOCK_SIZE):
                # load this line of data in, as many time as block size
                qr_bitmap._load_row(Y_OFFSET + y*BLOCK_SIZE+b, line) #pylint: disable=protected-access
        # pylint: enable=invalid-name

        # display the bitmap using our palette
        qr_sprite = displayio.Sprite(qr_bitmap, pixel_shader=palette, position=position)
        if self._qr_group:
            try:
                self._qr_group.pop()
            except IndexError: # later test if empty
                pass
        else:
            self._qr_group = displayio.Group()
            self.splash.append(self._qr_group)
        self._qr_group.append(qr_sprite)
        board.DISPLAY.refresh_soon()
        board.DISPLAY.wait_for_frame()

    # return a list of lines with wordwrapping
    @staticmethod
    def wrap_nicely(string, max_chars):
        """A list of lines with word wrapping.

        :param str string: The text to be wrapped.
        :param int max_chars: The maximum number of characters on a line before wrapping.

        """
        words = string.split(' ')
        the_lines = []
        the_line = ""
        for w in words:
            if len(the_line+' '+w) <= max_chars:
                the_line += ' '+w
            else:
                the_lines.append(the_line)
                the_line = ''+w
        if the_line:      # last line remaining
            the_lines.append(the_line)
        return the_lines
