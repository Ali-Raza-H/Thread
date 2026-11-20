

# 1. Dynamic analysis libraries research


### `Sys.Monitoring`

1. It monitors the interpreter of the program
2. performance varies depending on the number of events and type of event.
3. Does not inherinitly support behaviour changes
4. Only supports Python 3.12+
5. Doesn't inherntly alow you to see data such as variables and other in memory data

### `Sys.Settrace`

1. Trace execution through stack frames:
   - Stack frames in python refers to the data needed to run a function
   - in python when a function is called it creates a stack frame object containing all data to execute the function
   - Tracing this entails looking at the chain of active function calls in a program's memory in a specific moment in time (frame).
2. Has high performance overhead.
3. Inherintly supports pausing execution and stepping
4. At it's core it's an incredibly powerfull debugging library.

### `Sys.Setprofile`

1. Restricted version of  `sys.settrace`
2. Does'nt trigger on every line
3. Only tracks function entry and exit events
4. Much less performmance overhead than standard `settrace` due to limited tracking.
5. No line by line visiblity so extrem.ly limited live tracking
6. Best case scenario is shown as performance profiling which could be used to track time of execution for functions

### `Audit Hooks`

1. When an audit hook is called it recives two parameters as inputs
   - A string identifying the action such as open file connect socket or http client connection
   - `args` which is a  touple containing raw arguments passed to the operation such as the link for a http request among other things. It's essentially the opcode and operand in a high level programming language.
2. Allow opperations to be blcoked by raising permission error flags or custom exception routes
3. Behaviour of the prgroam can't be modified
4. Data can't be modified in runtime either

### `AST/Bytecode Adapterse`

1. Variable performance overhead, often with a high setup cost
2. Allows behaviour changes with two different approaches
   - `AST` Transformation
   - Bytecode transformation.

##### `AST` Transformation (Abstract Syntax Tree)
Parses python source code into a tree structure, then that hirearchal tree can be modified programatically. Then the modified tree can be compiled and turned back into executeable bytecode.
It allows code to be re written before the python interpreter executes it

**How it Works?**
1. Parsing using the `ast.parse()` function:
   `AST` Parse function is used with source code as a parameter.
   Outputs a root and nodes containing child nodes
2. Traversal and modification function is used to traverse the tree and modify child nodes to change the program's behaviour and data.
3. Location fixes. After tree modification new or modified child nodes locations may change or be overwritten and if compiled without fixing locations of nodes and child nodes the interpreter may throw errors.
4. Compilation. The modfied `AST` is passed to the python's built in compiler function which translates the `AST` directly into bytecode.
5. Execution. Bytecode is executed


### `Library Adapters`

1. Used to extract data from the different libraries and understand how they work
2. A lot more data can be extracted using a library adapter compared to a built in python function such as `sys.monitor`.
3. Avoids tracking uncessary internal library calls which produce empty noise not functional in the process of reverse engineering.
4. Each library needs it's own adapter.
5. Upddates to librries can break adapters.