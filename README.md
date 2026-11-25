# Short Order: a CPU scheduler that runs a restaurant kitchen

My scheduler for **Short Order**, the CPU-scheduling assignment in the Operating Systems course at the University of the Witwatersrand (2026).

In Short Order, a CPU scheduler runs a simulated restaurant kitchen:

| Kitchen | Operating system |
|---|---|
| Cook | CPU core |
| Order | Process |
| `work` step (chopping, plating) | CPU burst |
| `wait` step (the oven) | I/O burst |
| Customer's patience | Deadline |
| Taking a cook off one order and onto another | Context switch (costs ticks) |
| Station places (board, pass, bar) | Shared resources with limited capacity |

The engine calls the scheduler at every scheduling point (an order arrives, a cook comes free, a dish comes back from the oven, a customer walks out, or a timer fires). The scheduler must decide, within **50 ms**, which order each cook works on. It is scored on five things at once: customers served, response time, slowdown, time lost to switching, and fairness (Jain's index).

## Results

| | |
|---|---|
| Assignment mark | **100%** |
| Target | Beat `gordon_ramsay`, the strongest reference scheduler |
| Textbook algorithms | Scores higher than every textbook policy: FIFO, round robin, SJF, SRTF, priority, EDF, stride and MLFQ |
| Decision time | about 0.7 ms on average, well inside the 50 ms limit; no errors, timeouts or refused assignments |

`./start.sh compare my-scheduler` on the 10 default "lunch service" seeds:

| Scheduler | Algorithm | Score | Served | Fairness |
|---|---|---:|---:|---:|
| `gordon_ramsay` | the one to beat | 71.0 ± 0.9 | 95.2% | 0.563 |
| **`my_scheduler`** | **this repository** | **70.8 ± 0.9** | **95.0%** | **0.606** |
| `julia_child` | SJF | 68.4 | 94.4% | 0.474 |
| `sous_chef` | SRTF | 64.5 | 93.3% | 0.400 |
| `portion_control` | stride | 64.2 | 81.3% | 0.580 |
| `trainee` | EDF | 58.1 | 89.5% | 0.503 |
| `smoke_break` | MLFQ | 57.5 | 76.4% | 0.590 |
| `middle_management` | FIFO | 57.3 | 71.6% | 0.585 |
| `stickler` | priority | 55.3 | 79.6% | 0.454 |
| `headless_chicken` | round robin | 51.9 | 63.5% | 0.635 |

On these public seeds it is level with `gordon_ramsay` (the gap is inside the error margin). It shares the waiting out more evenly (fairness 0.606 against 0.563), and every one of its context switches is a necessary one.

## How the scheduler works

All the code is in [`my-scheduler/scheduler.py`](my-scheduler/scheduler.py). Every decision goes through these steps.

### 1. Rank orders by value per tick of work

Each waiting order gets a value:

```
value = 1 + 0.3 × (customer's patience / order's total time)
```

The order with the highest **value ÷ cook time still needed** is served first. If every order were worth the same, this would be plain **shortest-job-first**. The patience term tilts it towards orders that are quick but have patient customers (a salad, say). Losing one of those hurts slowdown and fairness far more than losing a long roast.

An order a cook has already started gets a bonus, so work already done is not thrown away when the customer leaves.

### 2. Never start a lost cause

An order is only started if the switch cost plus its remaining cook time and oven time fit before the customer walks out. Cook time that would be wasted goes to an order that can still be saved.

### 3. Respect station capacity

Some steps need a place at a station (the board, the pass, the bar). The scheduler counts free places and spends them as it assigns cooks, so the engine never has to refuse an assignment. In the test runs, 0 assignments were refused.

### 4. A short lookahead simulation

When a cook comes free, the top four candidate orders are each simulated **30 ticks ahead** with a small built-in model of the kitchen (cooks, ovens, stations, deadlines). The candidate whose future serves the most, loses the fewest and makes customers wait least is chosen. The lookahead has a time budget of 4 ms per decision and is skipped in very crowded kitchens, so it never risks the 50 ms deadline. If anything goes wrong inside it, the plain ranking is used.

### 5. Preempt only when it clearly pays

A context switch costs ticks, so a cook is taken off its order only when a waiting order is tiny next to the current one: under 10% of its remaining work (25% with a single cook, 30% for very long orders). When switching is almost free, the scheduler instead gives each brand-new order its first tick at once, which helps response time.

### 6. Fairness in a crowded kitchen

When the rail is very crowded, an order that would finish much faster than everyone else's is allowed to wait a little. Fairness is scored over slowdowns, so evening them out earns more than the small delay costs.

### 7. Kitchens with no oven and hidden durations

- **No oven steps:** with no I/O, nothing would wake the scheduler mid-order. So it sets its own timer and rotates cooks on short 2-tick slices, starting orders nobody has touched first. This is round robin, used only where it helps.
- **Hidden durations:** the `blind` profile hides how long each step takes. The scheduler then learns the average length of each step of each recipe from the orders already served, and estimates step by step.

## How it was developed

- Fourteen versions, each tested with `./start.sh evaluate` and `./start.sh compare` on many seeds against every reference scheduler.
- A change was kept only if it improved the mean score beyond the confidence interval.
- Each version was also tried on the other service profiles (rush, one cook, four cooks, banquet, bake-off, blind) so that it was not tuned to one kind of kitchen.
- Replays were watched in the browser viewer to see where customers walked out and why.

## Running it

This repository includes the course's **student platform** (the compiled kitchen engine, the `kitchen` Python package and a browser dashboard), so the scheduler runs straight after download. You need **Python 3.8 or newer**.

The engine in this distribution is compiled for **Linux x86-64**. On Windows, use **WSL** (Windows Subsystem for Linux), which is how I ran it. On macOS, use a Linux machine or a Docker container.

```bash
git clone https://github.com/pumelela1/short-order-scheduler.git
cd short-order-scheduler

bash start.sh                                # opens the dashboard in your browser
./start.sh play my-scheduler --seed 1001     # one service, with full statistics
./start.sh compare my-scheduler              # against every reference scheduler
./start.sh check my-scheduler                # the marker's own checks
./start.sh play my-scheduler --replay        # record a service and watch it
./start.sh play my-scheduler --config rush   # other profiles: one-cook, brigade, blind, ...
```

In the dashboard, pick `my-scheduler`, pick a service, and press **Play & watch**.

## What is mine and what is not

| Path | What it is | Written by |
|---|---|---|
| `my-scheduler/scheduler.py` | The scheduler: the file I submitted (version 14 of my work) | **Me** |
| `README.md` | This page | Me |
| Everything else (`engine/`, `student-sdk/`, `tools/`, `docs/`, `configs/`, `assets/`, `START.cmd`, `start.sh`, `launch.py`, `bootstrap.py`, `VERSION`) | The kitchen engine and student platform | **Wits course staff**, included so the scheduler can be run |

**Note on `student-sdk/kitchen/scheduler.py`:** my downloaded copy of the platform had lost this small base-class file. Only its compiled `.pyc` was left. I rebuilt it from that compiled copy so the platform runs. It defines the `Scheduler` base class and does not change how the engine scores anything.

The platform's own instructions are in [`docs/PLATFORM_README.md`](docs/PLATFORM_README.md) and [`docs/student-guide.md`](docs/student-guide.md).

## Author

**Pumelela Mapukata**, BSc Computer Science and Computational Applications, University of the Witwatersrand.
