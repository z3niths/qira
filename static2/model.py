from capstone import *
import capstone # for some unexported (yet) symbols in Capstone 3.0
import qira_config

if qira_config.WITH_BAP:
  import bap
  from bap import adt, arm, asm, bil
  from bap.adt import Visitor, visit
  from binascii import hexlify
  debug_level = 0

__all__ = ["Tags", "Function", "Block", "Instruction", "DESTTYPE","ABITYPE"]

class DESTTYPE(object):
  none = 0
  cjump = 1
  jump = 2
  call = 3
  implicit = 4

class Instruction(object):
  def __new__(cls, *args, **kwargs):
    if qira_config.WITH_BAP:
      try:
        return BapInsn(*args, **kwargs)
      except Exception as exn:
        print "bap failed", type(exn).__name__, exn
        return CsInsn(*args, **kwargs)
    else:
      return CsInsn(*args, **kwargs)

class BapInsn(object):
  def __init__(self, raw, address, arch):
    if len(raw) == 0:
      raise ValueError("Empty memory at {0:#x}".format(address))
    arch = 'armv7' if arch == 'arm' else arch
    insns = list(bap.disasm(raw,
                            addr=address,
                            arch=arch,
                            stop_conditions=[asm.Valid()]))
    if len(insns) == 0:
      raise ValueError("Invalid instruction for {1} at {2:#x}[{3}]:\n{0}".
                       format(hexlify(raw), arch, address, len(raw)))
    self.insn = insns[0]

    self.regs_read, self.regs_write = accesses(self.insn.bil)
    self.jumps = jumps(self.insn.bil)

    self.dtype = None
    if self.is_call():
      self.dtype = DESTTYPE.call
    elif self.is_conditional():
      self.dtype = DESTTYPE.cjump
    elif self.is_jump():
      self.dtype = DESTTYPE.jump

    dests = []

    if self.code_follows():
      dests.append((self.insn.addr + self.insn.size,
                    DESTTYPE.implicit))

    if self.insn.bil is not None:
      for (jmp,dtype) in self.jumps:
        if isinstance(jmp.arg, bil.Int):
          if self.is_call():
            dtype = DESTTYPE.call
          dests.append((jmp.arg.value, dtype))

    elif self.is_jump() or self.is_call():
      dst = self.insn.operands[0]
      #we want to check here if this is a relative or absolute jump
      #once we have BIL on x86 and x86-64 this won't matter
      if isinstance(dst, asm.Imm):
        dst_tmp = calc_offset(dst.arg, arch)
        if arch in ["i386","x86-64"]: #jump after instruction on x86, bap should tell us this
          dst_tmp += self.insn.size
        dests.append((dst_tmp + address, self.dtype))

    if self.is_ret():
      self._dests = []
    else:
      self._dests = dests

  def __str__(self):
    # fix relative jumps to absolute address
    for d in self._dests:
      if d[1] is not DESTTYPE.implicit:
        mnemonic = self.insn.asm.split("\t")[:-1] #ignore last operand
        mnemonic.append(hex(d[0]).strip("L")) #add destination to end
        newasm = "\t".join(mnemonic)
        return newasm
    return self.insn.asm

  def is_jump(self):
    if self.insn.bil is None:
      return self.insn.has_kind(asm.Branch)
    else:
      return len(self.jumps) != 0

  def is_hlt(self):
    return self.insn.asm == "\thlt" #x86 specific. BAP should be identifying this as an ending

  def is_ret(self):
    return self.insn.has_kind(asm.Return)

  def is_call(self):
    return self.insn.has_kind(asm.Call)

  def is_ending(self):
    if self.is_hlt() or self.is_ret():
      return True

    if self.insn.bil is None:
      return self.insn.has_kind(asm.Terminator)
    else:
      return self.insn.has_kind(asm.Terminator) or \
            (self.is_jump() and not self.is_call())

  def is_conditional(self):
    if self.insn.bil is None:
      return self.insn.has_kind(asm.Conditional_branch)
    else:
      for (_, dtype) in self.jumps:
        if dtype == DESTTYPE.cjump:
          return True
      return False

  def is_unconditional(self):
    if self.insn.bil is None:
      return self.insn.has_kind(asm.Unconditional_branch)
    else:
      if len(self.jumps) == 0:
        return False
      return not self.is_conditional()

  def code_follows(self):
    return self.is_call() or not (self.is_ret() or self.is_unconditional())

  def size(self):
    return self.insn.size

  def dests(self):
    return self._dests


def exists(cont,f):
  try:
    r = (x for x in cont if f(x)).next()
    return True
  except StopIteration:
    return False


if qira_config.WITH_BAP:
  class Jmp_visitor(Visitor):
    def __init__(self):
      self.in_condition = False
      self.jumps = []

    def visit_If(self, exp):
      was = self.in_condition
      self.in_condition = True
      self.run(exp.true)
      self.run(exp.false)
      self.in_condition = was

    def visit_Jmp(self, exp):
      self.jumps.append((exp,
                         DESTTYPE.cjump if self.in_condition else
                         DESTTYPE.jump))

  class Access_visitor(Visitor):
    def __init__(self):
        self.reads = []
        self.writes = []

    def visit_Move(self, stmt):
        self.writes.append(stmt.var.name)
        self.run(stmt.expr)

    def visit_Var(self, var):
        self.reads.append(var.name)

  def jumps(bil):
    return visit(Jmp_visitor(), bil).jumps

  def accesses(bil):
    r = visit(Access_visitor(), bil)
    return (r.reads, r.writes)

  #we could use ctypes here, but then we'd need an import
  def calc_offset(offset, arch):
    """
    Takes a 2's complement offset and, depending on the architecture,
    makes a Python value. See test_calc_offset for examples.

    Note: We may want to take the size of the int here, as x86-64 for
          example may use 32-bit ints when it uses x86 instructions.
    """
    if arch in ['aarch64', 'x86-64']:
      if (offset >> 63) & 1 == 1:
        #negative
        offset_fixed = -(0xFFFFFFFFFFFFFFFF-offset+1)
      else:
        offset_fixed = offset
    else:
      if offset != offset & 0xFFFFFFFF:
        if debug_level >= 1:
          print "[!] Warning: supplied offset 0x{:x} is not 32 bits.".format(offset)
      offset = offset & 0xFFFFFFFF
      if (offset >> 31) & 1 == 1:
        offset_fixed = -(0xFFFFFFFF-offset+1)
      else:
        offset_fixed = offset
    return offset_fixed

  def test_calc_offset():
    expected = {(0xFFFFFFFF, "x86"): -1,
                (0xFFFFFFFE, "x86"): -2,
                (0xFFFFFFFF, "x86-64"): 0xFFFFFFFF,
                (0xFFFFFFFF, "aarch64"): 0xFFFFFFFF,
                (0xFFFFFFFFFFFFFFFF, "x86-64"): -1,
                (0xFFFFFFFFFFFFFFFE, "x86-64"): -2}
    for k,v in expected.iteritems():
      v_prime = calc_offset(*k)
      if v_prime != v:
        k_fmt = (k[0],hex(k[1]),k[2])
        print "{0} -> {1:x} expected, got {0} -> {2:x}".format(k_fmt,v,v_prime)

class UnknownRegister(Exception):
  def __init__(self, reg):
    self.reg = reg

# Instruction class
class CsInsn(object):
  """one disassembled instruction"""
  def __init__(self, raw, address, arch):
    self.raw = raw
    self.address = address
    self.arch = arch
    if arch == "i386":
      self.md = Cs(CS_ARCH_X86, CS_MODE_32)
    elif arch == "x86-64":
      self.md = Cs(CS_ARCH_X86, CS_MODE_64)
    elif arch == "thumb":
      self.md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    elif arch == "arm":
      self.md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    elif arch == "aarch64":
      self.md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    elif arch == "ppc":
      self.md = Cs(CS_ARCH_PPC, CS_MODE_32 | CS_MODE_BIG_ENDIAN)
    elif arch == "mips":
      self.md = Cs(CS_ARCH_MIPS, CS_MODE_32 | CS_MODE_BIG_ENDIAN)
    elif arch == "mipsel":
      self.md = Cs(CS_ARCH_MIPS, CS_MODE_32 | CS_MODE_LITTLE_ENDIAN)
    else:
      raise Exception('arch "{}" not supported by capstone'.format(arch))
    self.md.detail = True
    try:
      self.i = self.md.disasm(self.raw, self.address).next()
      self.decoded = True
      self.regs_read = self.i.regs_read
      self.regs_write = self.i.regs_write

      self.dtype = DESTTYPE.none
      if arch == 'i386' or arch == 'x86-64':
          if self.i.mnemonic == "call":
            self.dtype = DESTTYPE.call
          elif self.i.mnemonic == "jmp":
            self.dtype = DESTTYPE.jump
          elif capstone.CS_GRP_JUMP in self.i.groups:
            self.dtype = DESTTYPE.cjump
      elif arch == "thumb" or arch == "arm" or arch == "aarch64":
        if self.i.mnemonic[:2] == "bl":
          self.dtype = DESTTYPE.call
        elif self.i.mnemonic == "b" or self.i.mnemonic == "bx":
          self.dtype = DESTTYPE.jump
        elif self.i.mnemonic[0] == "b" or self.i.mnemonic[:2] == "cb":
          self.dtype = DESTTYPE.cjump
      elif arch == "ppc":
        if self.i.mnemonic in ["bctr", "bctrl"]:
          self.dtype = DESTTYPE.none
        elif self.i.mnemonic[:2] == "bl":
          if not (len(self.i.mnemonic) > 3 and self.i.mnemonic[3] == "r"):
            self.dtype = DESTTYPE.call
        elif self.i.mnemonic[:2] == "bc":
          self.dtype = DESTTYPE.cjump
        elif self.i.mnemonic[0] == "b":
          self.dtype = DESTTYPE.jump
      elif arch == "mips" or arch == "mipsel":
        if self.i.mnemonic[:3] == "jal":
          self.dtype = DESTTYPE.call
        elif self.i.mnemonic == "j" or self.i.mnemonic == "jr" or self.i.mnemonic == "b":
          self.dtype = DESTTYPE.jump
        elif self.i.mnemonic[0] == "b":
          self.dtype = DESTTYPE.cjump

    #if capstone can't decode it, we're screwed
    except StopIteration:
      self.decoded = False

  def __repr__(self):
    return self.__str__()

  #we don't want to break str(x), but sometimes we want to augment the
  #diassembly with dynamic info. so we include optional arguments here
  def __str__(self, trace=None, clnum=None):
    if self.decoded:
      return "{}\t{}".format(self.i.mnemonic, self.get_operand_s(trace, clnum))
    return ""

  def is_jump(self):
    if not self.decoded:
      return False
    return self.dtype in [DESTTYPE.jump,DESTTYPE.cjump]

  def is_ret(self):
    arch = self.arch
    if not self.decoded:
      return False
    if arch == "i386" or arch == "x86-64":
      return self.i.mnemonic == "ret"
    elif arch == "thumb" or arch == "arm" or arch == "aarch64":
       is_branch_lr = self.i.mnemonic[0] == 'b' and self.i.op_str == "lr"
       is_pop_pc = self.i.mnemonic == "pop" and "pc" in self.i.op_str
       return is_branch_lr or is_pop_pc
    elif arch == "ppc":
      return self.i.mnemonic == "blr"
    elif arch == "mips" or arch == "mipsel":
      return self.i.mnemonic == "jr" and self.i.op_str == "$ra"

    #TODO: what about iret? and RET isn't in the apt version of capstone
    return capstone.CS_GRP_RET in self.i.groups

  def is_call(self):
    if not self.decoded:
      return False
    return self.dtype == DESTTYPE.call

  def is_ending(self):
    '''is this something which should end a basic block'''
    if not self.decoded:
      return False
    return self.is_jump() or self.is_ret() or self.i.mnemonic == "hlt"  # TODO: this is x86 specific

  def is_conditional(self):
    if not self.decoded:
      return False
    #TODO shouldn't be x86 specific
    return x86.X86_REG_EFLAGS in self.regs_read  # TODO: this is x86 specific

  def code_follows(self):
    '''should the data after this instructino be treated as code
       note that is_ending is different, as conditional jumps still have
       code that follows'''
    if not self.decoded:
      return False
    #code follows UNLESS we are a return or an unconditional jump
    return not (self.is_ret() or self.dtype == DESTTYPE.jump)

  def has_relative_reference(self):
    if not self.decoded:
      return False
    if self.arch in ["i386", "x86-64"]:
      has_ref = "[" in self.i.op_str and "]" in self.i.op_str
      uses_sp = "sp" in self.i.op_str.lower() or "bp" in self.i.op_str.lower()
      return has_ref and not uses_sp
    if self.arch in ["arm", "thumb", "aarch64"]:
      has_ref = "[" in self.i.op_str and "]" in self.i.op_str
      uses_sp = "sp" in self.i.op_str.lower()
      return has_ref and not uses_sp
    return False

  def get_operands(self):
    if self.arch in ["i386", "x86-64"]:
      return self.i.op_str.split(", ")
    raise Exception("unimplemented arch for get_operands: {}".format(self.arch))

  def get_operand_s(self, trace, clnum):
    if not self.has_relative_reference():
      return self.i.op_str
    print "special case! {} x {}".format(clnum, self.i.op_str)
    if self.arch in ["i386", "x86-64"]:
      operands = self.get_operands()
      return ", ".join(self.resolve_relative_reference(operand, trace, clnum) for operand in operands)
    if self.arch in ["arm", "aarch64", "thumb"]:
      op_str = self.i.op_str
      assert "[" in op_str
      assert op_str.count("[") == 1
      assert "]" in op_str and op_str.count("]") == 1
      ref = op_str[op_str.index("[")+1:op_str.index("]")]
      resolved = self.resolve_relative_reference(ref, trace, clnum)
      return "".join([op_str.split("[")[0]+"[", resolved, "]"+op_str.split("]")[1]])
    return self.i.op_str

  def resolve_relative_reference(self, operand, trace, clnum):
    if trace is None or clnum is None:
      #No register information, we can't resolve dynamic information.
      return operand

    program = trace.program
    registers = [reg.lower() for reg in program.tregs[0]]
    register_values = trace.db.fetch_registers(clnum)
    #ssert self.arch == program.tregs[-1] #this isn't true. see aarch64
    reginfo = dict(zip(registers, register_values))

    #check for overflow in here?
    def _eval_op_x86(exp):
      spl = exp.split(" ")
      if len(spl) == 3:
        op1, op, op2 = spl
        if op == "+":
          addr = _eval_op_x86(op1) + _eval_op_x86(op2)
        else:
          assert op == "-"
          addr = _eval_op_x86(op1) - _eval_op_x86(op2)
        return addr

      if len(spl) == 5:
        opr1, op1, opr2, op2, opr3 = spl
        addr = _eval_op_x86(opr1)
        if op1 == "+":
          addr += _eval_op_x86(opr2)
        else:
          assert op1 == "-"
          addr -= _eval_op_x86(opr2)
        if op2 == "+":
          addr += _eval_op_x86(opr3)
        else:
          assert op2 == "-"
          addr -= _eval_op_x86(opr3)
        return addr

      if "*" in exp:
        op1, op2 = exp.split("*")
        #assert len(spl2) == 2
        return _eval_op_x86(op1) * _eval_op_x86(op2)

      try:
        return int(exp, 16)
      except ValueError:
        if exp in reginfo: #it's a register
          return reginfo[exp]
        raise UnknownRegister(exp)

    def _eval_op_arm(exp):
      spl = exp.split(", ")
      if len(spl) == 2:
        op1, op2 = spl
        #print "recursing on", op1, op2
        return _eval_op_arm(op1) + _eval_op_arm(op2)
      if exp[0] == "#":
        return int(exp[1:], 16)
      if exp in reginfo: #it's a register
        if exp == "pc": #arm is so annoying
          return reginfo[exp] + self.size()
        return reginfo[exp]
      raise UnknownRegister(exp)

    if self.arch in ["i386", "x86-64"]:
      """
      looks like:
      qword ptr [rbp - 0x10]
      byte ptr [rip + 0x200a51]
      dword ptr [rax + rax]
      trace.fetch_raw_memory(clnum, addr, 1)
      """
      if "ptr" not in operand:
        return operand
      ref_size, ref = operand.split(" ptr ")
      #reference_sizes = {"qword": 8, "dword": 4, "byte": 1}
      ref = ref.replace("[", "").replace("]", "")
      try:
        return "{} ptr {}".format(ref_size, "[0x{:x}]".format(_eval_op_x86(ref)))
      except UnknownRegister:
        return operand
    elif self.arch in ["arm", "aarch64", "thumb"]:
      try:
        return "0x{:x}".format(_eval_op_arm(operand)) #need to clean this up
      except UnknownRegister as e:
        print "unknown reg! {}".format(e.reg)
        return operand
    else:
      print "*** Error: unsupported architecture {} for relative reference resolution!".format(self.arch)
      return operand

  def size(self):
    return self.i.size if self.decoded else 0

  def dests(self):
    if not self.decoded or self.is_ret():
      return []

    dl = []
    if self.code_follows():
      #this piece of code leads implicitly to the next instruction
      dl.append((self.address+self.size(),DESTTYPE.implicit))

    if self.is_jump() or self.is_call():
      #if we take a PTR and not a MEM or REG operand (TODO: better support for MEM operands)
      #TODO: shouldn't be x86 specific
      if (self.i.operands[0].type == capstone.CS_OP_IMM):
        dl.append((self.i.operands[0].value.imm,self.dtype)) #the target of the jump/call

    return dl


class ABITYPE(object):
  UNKNOWN       = ([],None)
  X86_CDECL     = ([],'EAX')
  X86_FASTCALL  = (['ECX','EDX'],'EAX')
  X86_BFASTCALL = (['EAX','EDX','ECX'],'EAX')
  X64_WIN       = (['RCX','RDX','R8', 'R9'],'RAX')
  X64_SYSV      = (['RDI','RSI','RDX','RCX','R8', 'R9'],'RAX')
  ARM_STD       = (['r0', 'r1', 'r2', 'r3'],'r0')

class Function:
  def __init__(self, start):
    self.start = start
    self.blocks = set()
    self.abi = 'UNKNOWN'
    self.nargs = 0

  def __repr__(self):
    return hex(self.start) + " " + str(self.blocks)

  def add_block(self, block):
    self.blocks.add(block)

  def update_abi(self, abi):
    self.abi = abi

class Block:
  def __init__(self, start):
    self.__start__ = start
    self.addresses = set([start])

  def __repr__(self):
    return hex(self.start())+"-"+hex(self.end())

  def start(self):
    return self.__start__

  def end(self):
    return max(self.addresses)

  def add(self, address):
    self.addresses.add(address)


class Tags:
  def __init__(self, static, address=None):
    self.backing = {}
    self.static = static
    self.address = address

  def __contains__(self, tag):
    return tag in self.backing

  def __getitem__(self, tag):
    if tag in self.backing:
      return self.backing[tag]
    else:
      # should reading the instruction tag trigger disasm?
      # and should dests be a seperate tag?
      if tag == "instruction":
        dat = self.static.memory(self.address, 0x10)
        # arch should probably come from the address with fallthrough
        self.backing['instruction'] = Instruction(dat, self.address, self.static[self.address]['arch'])
        self.backing['len'] = self.backing['instruction'].size()
        self.backing['type'] = 'instruction'
        return self.backing[tag]
      if tag == "crefs" or tag == "xrefs":
        # crefs has a default value of a new array
        self.backing[tag] = set()
        return self.backing[tag]
      if tag in self.static.global_tags:
        return self.static.global_tags[tag]
      return None

  def __delitem__(self, tag):
    try:
      del self.backing[tag]
    except:
      pass

  def __setitem__(self, tag, val):
    if tag == "instruction" and type(val) == str:
      raise Exception("instructions shouldn't be strings")
    if tag == "name":
      # name can change by adding underscores
      val = self.static.set_name(self.address, val)
    self.backing[tag] = val
