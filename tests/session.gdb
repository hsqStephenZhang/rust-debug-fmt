# gdb batch session used by tests/run.sh against tests/rfmt_test
set pagination off
set python print-stack full
source ../rust_debug_fmt_gdb.py

break rfmt_test::stop_here
run

echo ==== rargs (frame 0) ====\n
rargs

up
echo ==== rlocals (frame 1 = main) ====\n
rlocals

echo ==== rprint ====\n
rprint/p shape
rprint *p_ref p_ref.x arr[2] num ch big
printf "%s\n", $rfmt(tuple)

echo ==== process still healthy? ====\n
down
finish
continue

# keep this last: a gdb error aborts the rest of a batch script
echo ==== error path ====\n
rprint nodebug
