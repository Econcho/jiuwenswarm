#!/bin/bash
# ***********************************************************************
# Copyright: (c) Huawei Technologies Co., Ltd. 2019. All rights reserved.
# script for clean channel
# version: 1.0.0
# change log:
# ***********************************************************************
set -ex
set -o pipefail
python3 -m supervisor.supervisorctl stop all
exit 0