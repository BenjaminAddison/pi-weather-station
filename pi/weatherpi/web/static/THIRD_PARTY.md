# Third-party files in this folder

## chart.umd.min.js — Chart.js 4.4.7

- Project: https://www.chartjs.org (https://github.com/chartjs/Chart.js)
- Licence: MIT, Copyright (c) 2014-2024 Chart.js Contributors
- Source: `package/dist/chart.umd.js` from the npm tarball
  https://registry.npmjs.org/chart.js/-/chart.js-4.4.7.tgz (already minified upstream;
  renamed to `chart.umd.min.js`, otherwise byte-for-byte unchanged).
- sha256: `2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb`

Served locally by the dashboard (D24) so the page works when the viewing device has no
internet access. The trailing `sourceMappingURL` comment points at a map that isn't shipped;
browsers only request it with dev tools open, and a 404 there is harmless.

### MIT License

The MIT License (MIT)

Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
