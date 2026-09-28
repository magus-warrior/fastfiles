# Licensing

FastFiles uses the [FastFiles Personal Use License 1.0](../LICENSE), a custom
source-available license. The LICENSE file contains the controlling terms.
Copyright holder: **magus-warrior**.

## Free personal use

Individuals may use, inspect, modify, and share FastFiles without charge for
private, noncommercial purposes. Examples include transferring personal photos
between home computers, running a personal home lab, and learning from the code.
Redistributed copies and forks must retain the license and notices; modified
copies must be identified as modified. The same personal-use limits apply.

## Paid business and organizational use

A separate paid written license is required before using FastFiles for work,
a business, consulting, clients, or another organization. This includes internal
file transfers that do not directly earn money and use by nonprofits, schools,
and government organizations. The public license has no organizational exception.

Contact [magus-warrior](https://github.com/magus-warrior) through
[the FastFiles repository](https://github.com/magus-warrior/fastfiles) to arrange
a license. Pricing and permitted scope are agreed separately; no purchase flow,
support commitment, or commercial permission is implied by the public license.

FastFiles is described as **source available**, because open-source licenses must
allow commercial use. [OSI definition, section 6](https://opensource.org/osd)

## Contributions

Contributions must be compatible with the public license. Before incorporating
third-party contributions into a commercially licensed version, the maintainer
must also obtain sufficient rights from their copyright holders; the public
personal-use license alone does not grant those commercial rights.

## Third-party dependencies

Third-party libraries retain their own licenses. The installed versions reviewed
for this release report:

| Dependency | License reported by package metadata |
| --- | --- |
| PySide6 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| qt-material 2.17 | BSD-2-Clause |
| zeroconf 0.151.3 | LGPL-2.1-or-later |
| keyring 25.7.0 | MIT |

FastFiles uses the Qt Core, Gui, and Widgets modules. Review the exact libraries
and transitive dependencies you ship: a commercial FastFiles license does not
override their terms. In particular, LGPL distribution requires notices and
preserving users' rights concerning the LGPL libraries, including replacement
and source requirements. Qt also has GPL-only add-on modules. The current
installer obtains dependencies separately through pip; a future bundled desktop
installer needs its own distribution review.
[Qt's LGPL guidance](https://www.qt.io/development/open-source-lgpl-obligations)
