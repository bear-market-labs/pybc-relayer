#!/bin/bash

docker run -d --name pybc-relayer -p 6994:6994 -v /path/to/repos:/repos pybc-relayer:latest
