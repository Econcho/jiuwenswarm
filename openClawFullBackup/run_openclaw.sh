#!/bin/bash
# ***********************************************************************
# Copyright: (c) Huawei Technologies Co., Ltd. 2019. All rights reserved.
# script for build
# version: 1.0.0
# change log:
# ***********************************************************************
set -ex
set -o pipefail
pip config set global.index-url https://mirrors.huaweicloud.com/repository/pypi/simple
cd /home/sandbox/.openclaw/ && nohup python3 -m supervisor.supervisord -c /home/sandbox/supervisord.conf 2>&1 &
/var/paas/picod