video-analysis
==============
contains several modules and packages for doing video analysis with OpenCV and python.
The package is organized in multiple sub-packages:

video
-----
This package contains code, which can be used to process videos using python.
Special attention has been paid to develop video classes that can be easily
used in iterating over video frames, also with multiprocessing support.
The sub-package `analysis` contains several modules that contain miscellaneous
functions that might be useful for general image or video analysis

Part of the modules have been modified from code from moviepy, which
is released under the MIT license at github. The license is included
at the end of this file.

mousetracking
-------------
This package contains the actual video analysis code of the project within
which this package was developed.

lib
---
Small package that collects modules copied from other authors.



The MIT License (MIT) [OSI Approved License]
============================================

Copyright (c) 2014 Zulko

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
