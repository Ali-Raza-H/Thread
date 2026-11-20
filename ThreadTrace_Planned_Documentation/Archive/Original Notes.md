
## - What is it?

Thread Trace is meant to be a powerful reverse engineering tool for any type of python program. There will be 3 core functionalities as per following:

## 1. Static analysis

Static analysis is a Thread Trace mode where it allows the user to analyse the the code, functions , classes and modules of the python program. The program should strip down the code into categories for further analysis.

-  All the code must be broken down into categories such as functions, classes, modules (files that contain functions or classes that are imported into other files or the main file) etc...
- Libraries and variables used should also be categorised and displayed (as well as where they were used and the values they contain e.g inputs, return values etc...)
- It should catch and display arguments for any type of CLI applications as well. For example let's say take the Nmap tool and say it was made fully in python. It should take in the command used to run the project, the different arguments and flags available with each command and display what function each one of them runs. For example using the fully Python made Nmap let's say one of the commands is `Nmap ADDR -O`. In this scenario Nmap is the command to run the program with the `ADDR` as the argument and `-O` is the flag. ThreadTrace should display what part of the code each argument is used in and what functions each flag executes.
- For web applications routes and API's should be categorised as well. This means that the program should recognise the different routes coded in the web application and show what part of the code is rendered through which route. Furthermore, it should also recognise the different API's programmed into the program and what functions they call and through which routes the API's are called as well as any authentication functions and rate limiters placed on them.
- It should show the execution commands for the program analysed as well as the different forms of executing the program. In other words if it's a simple automation script the command could be something like `python main.py` or if it's an app run as a module it could be `python -m src.main --app` with different execution flags. They should all be displayed and point to the functions they run when executed. Furthermore it should also display the first function that's executed when the program starts. For example if a python program as the main function defined as `def main(n1, n2)` and it adds the two values entered as parameters and it's executed using the following syntax `if __name__ == __main__"` ThreadTrace should point at this line of code for the first execution. (I'm aware that python is an interpreted language and the interpreter will start at the first line of the file being executed but essentially the first line of code that will actually be executed in this scenario is this one and so ThreadTrace should point at it.)
- It should categorise algorithms it fins as well and calculate the space and time complexities of them in big O notation.
- It should allow the user to isolate functions and run them by themselves. For example let's say we have a function in a web application that takes in some data as parameters (or inputs) and does some calculation on them. The user should be able to isolate that one function and run it step by step to understand how it works and what it does.
- It should also track imports in this case I'm referring specifically to functions and variables imported from another file (i.e module) to a different file where it's actually being executed. This should be recorded to later be presented in a tree diagram demonstrating the relations between each file. (i.e module dependency diagram)
  The second part o this is that functions should also be tracked meaning the relation between defining functions and calling them should also be recorded (i.e where a function was defined where it was executed and with what variables or parameters in case it has some)
- Resources used by the program should also be tracked. Meaning any JSON files, images, audio files text files or databases should be tracked. This includes any external resource being used in the application. 
- Every file path should should also be tracked. Meaning if the python programs saves a file somewhere or imports a file somewhere that path should also be tracked as well as the thing is importing or saving.
- It should have a code editor built in to edit not only file's but also specific functions and classes. The editor shouldn't force the user to open the full file just to edit a specific function.
- Implementation for AI should also be considered to summarise functions, classes and files (on request) and explain other behaviour. It should have local LLM using Ollama with configurable model and Open AI python library compatible providers with configurable providers and models




## 2. Dynamic Analysis

Dynamic analysis is a Thread Trace mode where it allows the user to analyse the code it's functions classes and modules in runtime. The user will be able to see the code being executed in runtime line by line while it's being interpreted.

- Should be able to run a program using Thread Trace and enter a TUI where each line of the code is shown whilst is being run.
- There should be a mode for step by step analysis where the interpreter only moves to the next line by user input.
- The user should be able to pause move back and front while the program is being interpreted.
- There should be a feature to disable certain modules, functions and libraries temporarily on runtime.
- The user should also be able to run a program using Thread Trace on host mode and then launch another instance Thread Trace instance to listen on.
- For programs that send requests to the web it should also track those requests. Including data sent and to what address it's send to as well as what's received.
- For CLI application it should provider a terminal emulator where the CLI program can be run whilst its' being watched under Thread Trace.
- Thread Trace should also provider the code and interpreted runtime processess for the specific functions, methods and classes used by the program from libraries. (So instead of analysing the whole library only the specific functions and methods used for those libraries are tracked by Thread Trace)
- Allow the editing of variable contents in runtime to study behaviour.


## 3. Hybrid Analysis

Hybrid analysis will be the mode used to experiment on the program and study it's behaviour under different scenarios. It will be a mixture of code and dynamic analysis

- Once you run a program in hybrid it should be a mix of code analysis and runtime analysis
- It should allow the user to disable functions and libraries and run the program without them.
- It should allow the user to run specific functions to study they're behaviour in isolation to the rest of the code.
- It should allow the user to alter variables and another data in runtime to study the program's behaviour.
- Give the ability to modify functions and scripts (on a smal scale) to study alternative behaviours