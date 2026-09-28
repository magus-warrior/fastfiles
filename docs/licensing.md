# Licensing recommendation (proposal only)

Use **PolyForm Noncommercial 1.0.0** for free noncommercial use and offer a
separate paid commercial license for business use. The public license permits
noncommercial use, modification, and redistribution. Commercial rights come
from the separate agreement with the copyright holder.
[Official license text](https://polyformproject.org/licenses/noncommercial/1.0.0)

Describe this as **source available, free for noncommercial use**. Open source
licenses must permit commercial use; an MIT, GPL, or AGPL label cannot enforce
a rule that commercial users always pay.
[OSI definition, section 6](https://opensource.org/osd)

PolyForm explicitly permits some organizations, including education, charities,
public research, and government institutions. Confirm that these exceptions fit
the intended business model before adopting the license. If every organization
must pay, a reviewed custom agreement would be needed instead.

This file does not adopt PolyForm, set prices, or grant a license. Before adding
LICENSE and publishing licensing claims, identify the copyright holder, approve
the free-use scope, and establish commercial contact details and terms (such as
per-user/per-device coverage, redistribution, support, and upgrades). Obtain
appropriate rights for future contributions if they must also be offered under
the commercial agreement. A licensing lawyer should review the commercial terms
and dependency obligations before paid distribution.

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
