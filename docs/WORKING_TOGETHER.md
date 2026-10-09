# Best practices: several helpers, one project

You can ask more than one AI helper to work on the same software project.
This page is how we do that on HiveMind.
You can copy it.

You do not need to know how to code.
You do not need to be a GitHub expert.
GitHub is a website that stores a project and the notes people write about it.

A helper is an AI you talk to in a chat box.
You type what you want in normal sentences.
The helper does the computer work.

## Give each helper one job

One helper should not plan the work, build it, and then say it is good.
That helper would be grading their own homework.
Mistakes get missed.

Split the jobs.

**You are the owner.**
You say what you want.
You say yes or no on big changes.
You do not have to write the code.

**The planner writes the plan.**
The plan comes before any building.
On our project this helper is named Fable.

**The builder builds.**
The builder follows the plan after it is agreed.
On our project this helper is named Opus.

**The checker checks.**
The checker reads the plan.
The checker reads the finished work.
The checker does not build.
On our project this helper is named Grok.

You can use other helpers.
Keep the split.
The builder does not grade their own work.
The checker does not build.

## What to set up

You need four things.

1. A computer.
2. Three chats. One chat for each helper.
3. One project folder that all three can open.
4. A shared notebook. HiveMind is that notebook. When one helper learns a fact, the others can read it later. They do not start from a blank page each time.

A terminal is a window where someone types commands to the computer.
You do not have to open one.

Tell the builder this:

> Install HiveMind on this computer. Then tell me if the health check passed.

The builder uses the terminal.
You read the answer.

If the health check passed, the notebook is on.
Ask the builder to open the dashboard in your web browser.
The dashboard is a set of pages on your own computer.
You can look at the notebook there.
Those pages do not change it.

One computer is enough to start.
A second computer is better when you have one.
Ask the builder to install HiveMind there too, and to connect the two notebooks.
Agreement from two computers counts for more than two chats on one computer.

If you want the notes where other people can see them, you need a free account on GitHub.
You do not have to learn the site.
Tell the builder:

> Put this project on GitHub. Send me the web address.

You open that address in a browser when you want to read the notes.

## Tell them the rules on day one

Paste this to the planner:

> You are the planner. You write the plan in plain words. You do not build. You change the plan only when the checker disagrees, or when the checker finds something that must change. I am the owner. Ask me before you change the notebook's rules, how notes are signed, how computers share the notebook, or the security promises.

Paste this to the builder:

> You are the builder. You build only after the planner and the checker agree that nothing must change. You do not grade your own work. When you finish, say what you changed. I am the owner. Ask me before you change the notebook's rules, how notes are signed, how computers share the notebook, or the security promises.

Paste this to the checker:

> You are the checker. You read the plan once. You read the finished work. You do not build. You list what must change. If you find a new problem later, write it down. Write the word "blocks" only when that problem must stop the work. I am the owner. Ask me before you change the notebook's rules, how notes are signed, how computers share the notebook, or the security promises.

Paste this to all three:

> Before you save a note in the notebook, search it. Do not save the same fact twice. Do not save a guess as a fact. A guess stays a guess. Sign each note with your own name. Write the public notes on the project website so I can read them. Do not put passwords, private addresses, or long test logs in the notebook.

## How a piece of work moves

Use this order every time.

1. You say what you want, in normal sentences.
2. The planner writes a plan. The plan says what will change. It also says what will stay the same.
3. The checker reads that plan one time. The checker lists what must change.
4. The planner replies only if they disagree, or if there is a new must-change.
5. When nothing must change, the builder builds.
6. The computer runs its tests. A test is a small check the computer runs by itself. The main copy is the copy everyone treats as the real project. The main copy does not update until the required tests pass. On our project the required tests run on Ubuntu. Ubuntu is a kind of computer system, like Windows or Mac. A Mac test can run too. It does not have to pass.
7. The checker reads the finished work. The checker compares it to the plan.
8. If the checker finds a gap, the builder fixes that gap. The checker who found it looks again. Another checker looks again only if they had said the broken version was fine.
9. A new problem is written on the original note. It stops the work only when a checker writes the word "blocks".
10. When no must-change is left, and the required tests pass, the builder updates the main copy.

If a change is only tests, one checker says so.
The required tests still have to pass.

Sometimes two changes both edit the list of what changed.
The one that finishes second must start from the latest main list.
Then the two lists do not erase each other.

## After the main copy updates

Downloading the new files is not the last step.
The notebook program keeps running the old copy until someone restarts it.

Ask the checker to update the computer they use.
Ask the builder to update the other computer, if you have one.
Each of them should tell you the health check passed.

## What you must say yes to

You do not have to approve every small fix.

You do say yes before anyone builds a change to any of these:

- The notebook's rule set. Today that set is number 2.4.
- How notes are signed.
- How the computers share the notebook.
- The written security promises.

Two checkers agreeing does not replace your yes on those four.

## Where the notes go

Use both places.

The notebook is for facts the helpers must remember next time.
Each helper searches it at the start of work.

The project website is for notes you should see.
The plan goes there.
The check goes there.
The "this is done" message goes there.

A test passed, or a test failed, stays on the project website.
It does not go in the notebook.
A real bug does go in the notebook, as its own short fact.

## If you are stuck

Say which step you are on.
Say what you wanted.
Paste what the helper last said.
Ask the checker if the plan is ready for the builder.

The other pages in this folder are for helpers who build.
You can skip them.
Start here.
