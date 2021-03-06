import dis
from opcode import opmap, EXTENDED_ARG, HAVE_ARGUMENT
from types import CodeType
from _pydev_bundle import pydev_log
from _pydevd_bundle.pydevd_constants import IS_PY38_OR_GREATER

MAX_BYTE = 255
RETURN_VALUE_SIZE = 2


def _add_attr_values_from_insert_to_original(original_code, insert_code, insert_code_list, attribute_name, op_list):
    """
    This function appends values of the attribute `attribute_name` of the inserted code to the original values,
     and changes indexes inside inserted code. If some bytecode instruction in the inserted code used to call argument
     number i, after modification it calls argument n + i, where n - length of the values in the original code.
     So it helps to avoid variables mixing between two pieces of code.

    :param original_code: code to modify
    :param insert_code: code to insert
    :param insert_code_obj: bytes sequence of inserted code, which should be modified too
    :param attribute_name: name of attribute to modify ('co_names', 'co_consts' or 'co_varnames')
    :param op_list: sequence of bytecodes whose arguments should be changed
    :return: modified bytes sequence of the code to insert and new values of the attribute `attribute_name` for
    original code
    """
    orig_value = getattr(original_code, attribute_name)
    insert_value = getattr(insert_code, attribute_name)
    orig_names_len = len(orig_value)
    code_with_new_values = list(insert_code_list)
    offset = 0
    while offset < len(code_with_new_values):
        op = code_with_new_values[offset]
        if op in op_list:
            new_val = code_with_new_values[offset + 1] + orig_names_len
            if new_val > MAX_BYTE:
                code_with_new_values[offset + 1] = new_val & MAX_BYTE
                code_with_new_values = code_with_new_values[:offset] + [EXTENDED_ARG, new_val >> 8] + \
                                       code_with_new_values[offset:]
                offset += 2
            else:
                code_with_new_values[offset + 1] = new_val
        offset += 2
    new_values = orig_value + insert_value
    return bytes(code_with_new_values), new_values


def _modify_new_lines(code_to_modify, offset, code_to_insert):
    """
    Update new lines: the bytecode inserted should be the last instruction of the previous line.
    :return: bytes sequence of code with updated lines offsets
    """
    # There's a nice overview of co_lnotab in
    # https://github.com/python/cpython/blob/3.6/Objects/lnotab_notes.txt

    new_list = list(code_to_modify.co_lnotab)
    if not new_list:
        # Could happen on a lambda (in this case, a breakpoint in the lambda should fallback to
        # tracing).
        return None

    # As all numbers are relative, what we want is to hide the code we inserted in the previous line
    # (it should be the last thing right before we increment the line so that we have a line event
    # right after the inserted code).
    bytecode_delta = len(code_to_insert)

    byte_increments = code_to_modify.co_lnotab[0::2]
    line_increments = code_to_modify.co_lnotab[1::2]

    if offset == 0:
        new_list[0] += bytecode_delta
    else:
        addr = 0
        it = zip(byte_increments, line_increments)
        for i, (byte_incr, _line_incr) in enumerate(it):
            addr += byte_incr
            if addr == offset:
                new_list[i * 2] += bytecode_delta
                break

    return bytes(new_list)


def _unpack_opargs(code, inserted_code_list, current_index):
    """
    Modified version of `_unpack_opargs` function from module `dis`.
    We have to use it, because sometimes code can be in an inconsistent state: if EXTENDED_ARG
    operator was introduced into the code, but it hasn't been inserted into `code_list` yet.
    In this case we can't use standard `_unpack_opargs` and we should check whether there are
    some new operators in `inserted_code_list`.
    """
    extended_arg = 0
    for i in range(0, len(code), 2):
        op = code[i]
        if op >= HAVE_ARGUMENT:
            if not extended_arg:
                # in case if we added EXTENDED_ARG, but haven't inserted it to the source code yet.
                for code_index in range(current_index, len(inserted_code_list)):
                    inserted_offset, inserted_code = inserted_code_list[code_index]
                    if inserted_offset == i and inserted_code[0] == EXTENDED_ARG:
                        extended_arg = inserted_code[1] << 8
            arg = code[i + 1] | extended_arg
            extended_arg = (arg << 8) if op == EXTENDED_ARG else 0
        else:
            arg = None
        yield (i, op, arg)


def _update_label_offsets(code_obj, breakpoint_offset, breakpoint_code_list):
    """
    Update labels for the relative and absolute jump targets
    :param code_obj: code to modify
    :param breakpoint_offset: offset for the inserted code
    :param breakpoint_code_list: size of the inserted code
    :return: bytes sequence with modified labels; list of tuples (resulting offset, list of code instructions) with
    information about all inserted pieces of code
    """
    inserted_code = list()
    # the list with all inserted pieces of code
    inserted_code.append((breakpoint_offset, breakpoint_code_list))
    code_list = list(code_obj)
    j = 0

    while j < len(inserted_code):
        current_offset, current_code_list = inserted_code[j]
        offsets_for_modification = []

        for offset, op, arg in _unpack_opargs(code_list, inserted_code, j):
            if arg is not None:
                if op in dis.hasjrel:
                    # has relative jump target
                    label = offset + 2 + arg
                    if offset < current_offset < label:
                        # change labels for relative jump targets if code was inserted inside
                        offsets_for_modification.append(offset)
                elif op in dis.hasjabs:
                    # change label for absolute jump if code was inserted before it
                    if current_offset < arg:
                        offsets_for_modification.append(offset)
        for i in range(0, len(code_list), 2):
            op = code_list[i]
            if i in offsets_for_modification and op >= dis.HAVE_ARGUMENT:
                new_arg = code_list[i + 1] + len(current_code_list)
                if new_arg <= MAX_BYTE:
                    code_list[i + 1] = new_arg
                else:
                    # handle bytes overflow
                    if i - 2 > 0 and code_list[i - 2] == EXTENDED_ARG and code_list[i - 1] < MAX_BYTE:
                        # if new argument > 255 and EXTENDED_ARG already exists we need to increase it's argument
                        code_list[i - 1] += 1
                    else:
                        # if there isn't EXTENDED_ARG operator yet we have to insert the new operator
                        extended_arg_code = [EXTENDED_ARG, new_arg >> 8]
                        inserted_code.append((i, extended_arg_code))
                    code_list[i + 1] = new_arg & MAX_BYTE

        code_list = code_list[:current_offset] + current_code_list + code_list[current_offset:]

        for k in range(len(inserted_code)):
            offset, inserted_code_list = inserted_code[k]
            if current_offset < offset:
                inserted_code[k] = (offset + len(current_code_list), inserted_code_list)
        j += 1

    return bytes(code_list), inserted_code


def _return_none_fun():
    return None


def add_jump_instruction(jump_arg, code_to_insert):
    """
    Note: although it's adding a POP_JUMP_IF_TRUE, it's actually no longer used now
    (we could only return the return and possibly the load of the 'None' before the
    return -- not done yet because it needs work to fix all related tests).
    """
    extended_arg_list = []
    if jump_arg > MAX_BYTE:
        extended_arg_list += [EXTENDED_ARG, jump_arg >> 8]
        jump_arg = jump_arg & MAX_BYTE

    # remove 'RETURN_VALUE' instruction and add 'POP_JUMP_IF_TRUE' with (if needed) 'EXTENDED_ARG'
    return list(code_to_insert.co_code[:-RETURN_VALUE_SIZE]) + extended_arg_list + [opmap['POP_JUMP_IF_TRUE'], jump_arg]


_created = {}


def insert_code(code_to_modify, code_to_insert, before_line, all_lines_with_breaks=()):
    '''
    :param all_lines_with_breaks:
        tuple(int) a tuple with all the breaks in the given code object (this method is expected
        to be called multiple times with different lines to add multiple breakpoints, so, the
        variable `before_line` should have the current breakpoint an the all_lines_with_breaks
        should have all the breakpoints added so far (including the `before_line`).
    '''
    if not all_lines_with_breaks:
        # Backward-compatibility with signature which received only one line.
        all_lines_with_breaks = (before_line,)

    # The cache is needed for generator functions, because after each yield a new frame
    # is created but the former code object is used (so, check if code_to_modify is
    # already there and if not cache based on the new code generated).

    # print('inserting code', before_line, all_lines_with_breaks)
    # dis.dis(code_to_modify)

    ok_and_new_code = _created.get((code_to_modify, all_lines_with_breaks))
    if ok_and_new_code is not None:
        return ok_and_new_code

    ok, new_code = _insert_code(code_to_modify, code_to_insert, before_line)

    # print('insert code ok', ok)
    # dis.dis(new_code)

    # Note: caching with new code!
    cache_key = new_code, all_lines_with_breaks
    _created[cache_key] = (ok, new_code)
    return _created[cache_key]


def _insert_code(code_to_modify, code_to_insert, before_line):
    """
    Insert piece of code `code_to_insert` to `code_to_modify` right inside the line `before_line` before the
    instruction on this line by modifying original bytecode

    :param code_to_modify: Code to modify
    :param code_to_insert: Code to insert
    :param before_line: Number of line for code insertion
    :return: boolean flag whether insertion was successful, modified code
    """
    linestarts = dict(dis.findlinestarts(code_to_modify))
    if not linestarts:
        return False, code_to_modify

    if code_to_modify.co_name == '<module>':
        # There's a peculiarity here: if a breakpoint is added in the first line of a module, we
        # can't replace the code because we require a line event to stop and the line event
        # was already generated, so, fallback to tracing.
        if before_line == min(linestarts.values()):
            return False, code_to_modify

    if before_line not in linestarts.values():
        return False, code_to_modify

    offset = None
    for off, line_no in linestarts.items():
        if line_no == before_line:
            offset = off
            break

    code_to_insert_list = add_jump_instruction(offset, code_to_insert)
    try:
        code_to_insert_list, new_names = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_list, 'co_names',
                                                     dis.hasname)
        code_to_insert_list, new_consts = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_list, 'co_consts',
                                                     [opmap['LOAD_CONST']])
        code_to_insert_list, new_vars = \
            _add_attr_values_from_insert_to_original(code_to_modify, code_to_insert, code_to_insert_list, 'co_varnames',
                                                     dis.haslocal)
        new_bytes, all_inserted_code = _update_label_offsets(code_to_modify.co_code, offset, list(code_to_insert_list))

        new_lnotab = _modify_new_lines(code_to_modify, offset, code_to_insert_list)
        if new_lnotab is None:
            return False, code_to_modify

    except ValueError:
        pydev_log.exception()
        return False, code_to_modify

    args = [
        code_to_modify.co_argcount,  # integer
    ]

    if IS_PY38_OR_GREATER:
        # New argument on Python 3.8
        args.append(
            code_to_modify.co_posonlyargcount,  # integer
        )

    args.extend((
        code_to_modify.co_kwonlyargcount,  # integer
        len(new_vars),  # integer
        code_to_modify.co_stacksize,  # integer
        code_to_modify.co_flags,  # integer
        new_bytes,  # bytes
        new_consts,  # tuple
        new_names,  # tuple
        new_vars,  # tuple
        code_to_modify.co_filename,  # string
        code_to_modify.co_name,  # string
        code_to_modify.co_firstlineno,  # integer
        new_lnotab,  # bytes
        code_to_modify.co_freevars,  # tuple
        code_to_modify.co_cellvars  # tuple
    ))

    new_code = CodeType(*args)
    return True, new_code
