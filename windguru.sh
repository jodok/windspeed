#!/bin/bash
cd "$(dirname "$0")"
sleep 15
.venv/bin/python windguru.py --station "$1"
