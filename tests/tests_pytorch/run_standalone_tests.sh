#!/bin/bash
# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
set -e
# THIS FILE ASSUMES IT IS RUN INSIDE THE tests/tests_pytorch DIRECTORY

# Batch size for testing: Determines how many standalone test invocations run in parallel
test_batch_size=6

#while getopts "b:" opt; do
#    case $opt in
#        b)
#            test_batch_size=$OPTARG;;
#        *)
#            echo "Usage: $(basename $0) [-b batch_size]"
#            exit 1;;
#    esac
#done
#shift $((OPTIND-1))

# this environment variable allows special tests to run
export PL_RUN_STANDALONE_TESTS=1
# python arguments
defaults='-m coverage run --source pytorch_lightning --append -m pytest --no-header'

# find tests marked as `@RunIf(standalone=True)`. done manually instead of with pytest because it is faster
grep_output=$(grep --recursive --word-regexp . --regexp 'standalone=True' --include '*.py')

# file paths, remove duplicates
files=$(echo "$grep_output" | cut -f1 -d: | sort | uniq)

# get the list of parametrizations. we need to call them separately. the last two lines are removed.
# note: if there's a syntax error, this will fail with some garbled output
if [[ "$OSTYPE" == "darwin"* ]]; then
  parametrizations=$(python -m pytest $files --collect-only --quiet "$@" | tail -r | sed -e '1,3d' | tail -r)
else
  parametrizations=$(python -m pytest $files --collect-only --quiet "$@" | head -n -2)
fi
# remove the "tests/tests_pytorch" path suffixes
parametrizations=${parametrizations//"tests/tests_pytorch/"/}
parametrizations_arr=($parametrizations)

# tests to skip - space separated
blocklist='profilers/test_profiler.py::test_pytorch_profiler_nested_emit_nvtx utilities/test_warnings.py'
report=''

rm -f standalone_test_output.txt  # in case it exists, remove it
function show_batched_output {
  if [ -f standalone_test_output.txt ]; then  # if exists
    cat standalone_test_output.txt
    rm standalone_test_output.txt
  fi
}
trap show_batched_output EXIT  # show the output on exit

for i in "${!parametrizations_arr[@]}"; do
  parametrization=${parametrizations_arr[$i]}

  # check blocklist
  if echo $blocklist | grep -F "${parametrization}"; then
    report+="Skipped\t$parametrization\n"
    # do not continue the loop because we might need to wait for batched jobs
  else
    echo "Running $parametrization"
    # execute the test in the background
    # redirect to a log file that buffers test output. since the tests will run in the background, we cannot let them
    # output to std{out,err} because the outputs would be garbled together
    python ${defaults} "$parametrization" &>> standalone_test_output.txt &
    # save the PID in an array
    pids[${i}]=$!
    # add row to the final report
    report+="Ran\t$parametrization\n"
  fi

  if ((($i + 1) % $test_batch_size == 0)); then
    # wait for running tests
    for pid in ${pids[*]}; do wait $pid; done
    unset pids  # empty the array
    show_batched_output
  fi
done
# wait for leftover tests
for pid in ${pids[*]}; do wait $pid; done
show_batched_output
echo "Batched mode finished. Continuing with the rest of standalone tests."

if nvcc --version; then
    nvprof --profile-from-start off -o trace_name.prof -- python ${defaults} profilers/test_profiler.py::test_pytorch_profiler_nested_emit_nvtx
fi

# needs to run outside of `pytest`
python utilities/test_warnings.py
if [ $? -eq 0 ]; then
    report+="Ran\tutilities/test_warnings.py\n"
fi

# test deadlock is properly handled with TorchElastic.
LOGS=$(PL_RUN_STANDALONE_TESTS=1 PL_RECONCILE_PROCESS=1 python -m torch.distributed.run --nproc_per_node=2 --max_restarts 0 -m coverage run --source pytorch_lightning -a plugins/environments/torch_elastic_deadlock.py | grep "SUCCEEDED")
if [ -z "$LOGS" ]; then
    exit 1
fi
report+="Ran\tplugins/environments/torch_elastic_deadlock.py\n"

# test that a user can manually launch individual processes
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
args="--trainer.accelerator gpu --trainer.devices 2 --trainer.strategy ddp --trainer.max_epochs=1 --trainer.limit_train_batches=1 --trainer.limit_val_batches=1 --trainer.limit_test_batches=1"
MASTER_ADDR="localhost" MASTER_PORT=1234 LOCAL_RANK=1 python ../../examples/convert_from_pt_to_pl/image_classifier_5_lightning_datamodule.py ${args} &
MASTER_ADDR="localhost" MASTER_PORT=1234 LOCAL_RANK=0 python ../../examples/convert_from_pt_to_pl/image_classifier_5_lightning_datamodule.py ${args}
report+="Ran\tmanual ddp launch test\n"

# echo test report
printf '=%.s' {1..80}
printf "\n$report"
printf '=%.s' {1..80}
printf '\n'
