# OTUServer

Sample asynchronous web server. 

# How to use
To start the server, use the following command:
```shell
$ python httpd.py [-w NUMBER_OF_WORKERS] [-r DOCUMENT_ROOT]
```
If ``NUMBER_OF_WORKERS`` is not specified, two worker processes will be created; if ``DOCUMENT_ROOT`` is omitted - the 
current directory will be used as the document root.

Port 8080 must not be used by any other process. To check that the server is running, enter ``http://localhost:8080/``
in browser. Contents of the ``DOCUMENT_ROOT/index.html`` or the empty page should be displayed.

# Features
* Delivery of static content only
* Processing ``GET`` and ``HEAD`` requests only
* Ability to run multiple worker processes with an asynchronous loop in each of them
* Processing percent-encoded urls

# Requirements
* Python ≥ 3.6 is required
* Server uses ``epoll()`` system call supported only on Linux 2.5.44 and newer

# Stress testing
Stress testing is performed using the ``ab`` utility. In Ubuntu it is available as a part of the ``apache2-utils`` 
package:
```shell
$ sudo apt install apache2-utils
```

To start testing, use the following command:
```shell
$ ab -n 50000 -c 100 http://127.0.0.1:8080/
```

Sample output:
```log
This is ApacheBench, Version 2.3 <$Revision: 1807734 $>
Copyright 1996 Adam Twiss, Zeus Technology Ltd, http://www.zeustech.net/
Licensed to The Apache Software Foundation, http://www.apache.org/

Benchmarking 127.0.0.1 (be patient)
Completed 5000 requests
Completed 10000 requests
Completed 15000 requests
Completed 20000 requests
Completed 25000 requests
Completed 30000 requests
Completed 35000 requests
Completed 40000 requests
Completed 45000 requests
Completed 50000 requests
Finished 50000 requests


Server Software:        OTUServer
Server Hostname:        127.0.0.1
Server Port:            5000

Document Path:          /
Document Length:        12 bytes

Concurrency Level:      100
Time taken for tests:   63.229 seconds
Complete requests:      50000
Failed requests:        0
Total transferred:      9750000 bytes
HTML transferred:       600000 bytes
Requests per second:    790.78 [#/sec] (mean)
Time per request:       126.458 [ms] (mean)
Time per request:       1.265 [ms] (mean, across all concurrent requests)
Transfer rate:          150.59 [Kbytes/sec] received

Connection Times (ms)
              min  mean[+/-sd] median   max
Connect:        0    0   0.1      0       4
Processing:    15  126  41.5    117     746
Waiting:       15  126  41.5    117     746
Total:         19  126  41.5    117     746

Percentage of the requests served within a certain time (ms)
  50%    117
  66%    125
  75%    131
  80%    136
  90%    160
  95%    195
  98%    217
  99%    255
 100%    746 (longest request)
```