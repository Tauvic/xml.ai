#! /bin/sh

export PYTHONPATH=$(pwd)/hier2hier
# N.B.: assumes script is called from parent directory, as described in README.md
cd scripts
python generate_toy_data.py $*
cd ..
