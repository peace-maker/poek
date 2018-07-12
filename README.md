# Poke + peek = poek

The popular `peek` and `poke` lives again!

This time in an even more user-friendly version; If the poker and the peeker are
located within UDP broadcast range, they will automatically discover each other.

Both `peek` and `poke` are completely self contained Python scripts (though they
depend on [Pwntools](https://github.com/Gallopsled/pwntools)).

## `poke`

Pretty simple:  To start serving files `foo`, `bar` and `baz` just run:

```sh
$ poke foo bar baz
```

If for some reason you don't want to use the default port (1337, what else), you
can change that with the `--port` option

## `peek`

Just run:

```sh
$ peek
```

If one or more `poke` instances are discovered, a file list will be fetched and
shown.  Navigate the list with the arrow keys and hit space or enter to download
the selected file.  Hit `h` for a list of shortcuts.

You can connect directly to a `poke` instance by specifying its address on the
command line:

```sh
$ peek <far-away-host>
```

This is useful if the host is not within UDP broadcast range.

As with `poke` the port number can be changed with the `--port` option.

## `nc`

Poke sends the contents of files without any meta data and directories as
regular tar archives.  In other words, if you don't have `peek` installed you
can just `nc` to the poker on the port corresponding to the desired file or
directory:

```
$ nc <host> 1337 > file01 # first file in list
$ nc <host> 1338 > file02 # second file, ...
```
