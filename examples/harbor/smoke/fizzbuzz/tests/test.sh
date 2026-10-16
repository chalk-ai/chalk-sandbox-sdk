#!/bin/bash
mkdir -p /logs/verifier
expected=$(python3 -c 'print("\n".join("FizzBuzz" if i%15==0 else "Fizz" if i%3==0 else "Buzz" if i%5==0 else str(i) for i in range(1,31)))')
actual=$(python3 /app/fizzbuzz.py 2>/dev/null)
[ "$actual" = "$expected" ] && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
