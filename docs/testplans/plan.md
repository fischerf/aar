You are given a small project. Your goal is to FULLY complete the task.

IMPORTANT:
- Do not stop early.
- Do not explain what you would do — DO it.
- Only finish when everything works and all requirements are satisfied.

---

PROJECT SETUP:

There is a folder with these files:

1. main.py
2. utils.py

Contents:

--- main.py ---
from utils import process_numbers

def main():
    nums = [1, 2, 3, 4]
    result = process_numbers(nums)
    print(result)

if __name__ == "__main__":
    main()

--- utils.py ---
def process_numbers(nums):
    # TODO: implement
    pass

---

TASK:

1. Implement `process_numbers(nums)` so that:
   - It returns a list where:
     - even numbers are doubled
     - odd numbers are squared

   Example:
   Input: [1,2,3]
   Output: [1,4,9]

2. Create a new file `test_utils.py` that:
   - tests at least 3 cases
   - exits with a non-zero code if a test fails

3. Modify `main.py` so that:
   - it prints "DONE" after printing the result

4. Run the program and tests using bash to verify everything works.

5. If anything fails:
   - fix it
   - re-run until it passes

6. When everything works:
   - print EXACTLY: ALL_TASKS_COMPLETED

---

CONSTRAINTS:

- You MUST use the available tools (read_file, write_file, edit_file, bash).
- Do NOT assume correctness — VERIFY by running code.
- Do NOT stop after writing code — you must run and confirm.
- Do NOT stop if something fails — fix it.

---

SUCCESS CONDITION:

The task is only complete if:
- implementation is correct
- tests pass
- program runs
- "DONE" is printed
- AND you output: ALL_TASKS_COMPLETED
