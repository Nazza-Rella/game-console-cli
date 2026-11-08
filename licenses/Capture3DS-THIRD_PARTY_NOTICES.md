# Third-party notices

The MIT License in [LICENSE](LICENSE) applies to `capture3ds-sharp` itself.
The external runtime libraries listed below remain under their own licences
and are not relicensed by this project. They are intentionally not committed
to this repository.

## FTDI D2XX and D3XX

`ftd2xx.dll` and `FTD3XX.dll` are proprietary FTDI driver libraries. They may
be used only with products based on genuine FTDI components and are subject
to FTDI's driver licence terms. Obtain them from the official
[D2XX](https://ftdichip.com/drivers/d2xx-drivers/) and
[D3XX](https://ftdichip.com/drivers/d3xx-drivers/) pages and review the
[FTDI Driver Licence Terms](https://ftdichip.com/driver-licence-terms-details/).

## Cypress CyUSB

`CyUSB.dll` is proprietary Cypress/Infineon software. It is not open source
and is subject to the Cypress Software License Agreement supplied with the
USB Suite. Its use and object-code redistribution are limited to software
supporting a product used in conjunction with a Cypress integrated circuit.
Source code must not be redistributed. This project uses it only for the
Cypress EZ-USB FX2 controller in LL-SPA3 capture hardware.

## libusb

`libusb-1.0.dll` is licensed under the GNU Lesser General Public License,
version 2.1 or (at your option) any later version. The DLL is dynamically
loaded as a replaceable shared library. See the
[libusb licence](https://github.com/libusb/libusb/blob/master/COPYING) and
[source repository](https://github.com/libusb/libusb).

The NX binary distribution currently uses the Windows x64 DLL from
`libusb-package 1.0.26.4`, built from libusb commit
`ba698478afc3d3a72644eef9fc4cd24ce8383a4c`. That binary distribution includes
the LGPL text and the exact corresponding source and build-script archives.
The `libusb-package` build wrapper is separately licensed under Apache-2.0.

## Protocol references

- [cc3dsfs](https://github.com/Lorenzooone/cc3dsfs), MIT License,
  Copyright (c) Lorenzooone.
- [CuteCapture](https://github.com/Gotos/CuteCapture), Apache License 2.0,
  used as a reference for the Loopy original-3DS protocol and image layout.
  Its licence text is included at
  [LICENSES/CuteCapture-Apache-2.0.txt](LICENSES/CuteCapture-Apache-2.0.txt).
