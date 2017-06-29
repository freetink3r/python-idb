'''
lots of inspiration from: https://github.com/nlitsme/pyidbutil
'''
import abc
import struct
import logging
from collections import namedtuple

import vstruct
from vstruct.primitives import v_bytes
from vstruct.primitives import v_uint8
from vstruct.primitives import v_uint16
from vstruct.primitives import v_uint32
from vstruct.primitives import v_uint64

import idb.netnode


logger = logging.getLogger(__name__)


class FileHeader(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        # list of offsets to section headers.
        # order should line up with the SECTIONS definition (see below).
        self.offsets = []
        # list of checksums of sections.
        # order should line up with the SECTIONS definition.
        self.checksums = []

        self.signature = v_bytes(size=0x4)  # IDA1
        self.unk04 = v_uint16()
        self.offset1 = v_uint64()
        self.offset2 = v_uint64()
        self.unk16 = v_uint32()
        self.sig2 = v_uint32()  # | DD CC BB AA |
        self.version = v_uint16()
        self.offset3 = v_uint64()
        self.offset4 = v_uint64()
        self.offset5 = v_uint64()
        self.checksum1 = v_uint32()
        self.checksum2 = v_uint32()
        self.checksum3 = v_uint32()
        self.checksum4 = v_uint32()
        self.checksum5 = v_uint32()
        self.offset6 = v_uint64()
        self.checksum6 = v_uint32()

    def pcb_version(self):
        if self.version != 0x6:
            raise NotImplementedError('unsupported version: %d' % (self.version))

    def pcb_offset6(self):
        self.offsets.append(self.offset1)
        self.offsets.append(self.offset2)
        self.offsets.append(self.offset3)
        self.offsets.append(self.offset4)
        self.offsets.append(self.offset5)
        self.offsets.append(self.offset6)

    def pcb_checksum6(self):
        self.checksums.append(self.checksum1)
        self.checksums.append(self.checksum2)
        self.checksums.append(self.checksum3)
        self.checksums.append(self.checksum4)
        self.checksums.append(self.checksum5)
        self.checksums.append(self.checksum6)

    def validate(self):
        if self.signature != b'IDA1':
            raise ValueError('bad signature')
        if self.sig2 != 0xAABBCCDD:
            raise ValueError('bad sig2')
        if self.version != 0x6:
            raise ValueError('unsupported version')
        return True


class SectionHeader(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.is_compressed = v_uint8()
        self.length = v_uint64()


class Section(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.header = SectionHeader()
        self.contents = v_bytes()

    def pcb_header(self):
        if self.header.is_compressed:
            # TODO: support this.
            raise NotImplementedError('compressed section')

        self['contents'].vsSetLength(self.header.length)

    def validate(self):
        if self.header.length == 0:
            raise ValueError('zero size')
        return True


# sizeof(BranchEntryPointer)
# sizeof(BranchEntry)
# sizeof(LeafEntry)
# sizeof(LeafEntryPointer)
SIZEOF_ENTRY = 0x6


class BranchEntryPointer(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.page = v_uint32()
        self.offset = v_uint16()


class BranchEntry(vstruct.VStruct):
    def __init__(self, page):
        vstruct.VStruct.__init__(self)
        self.page = page
        self.key_length = v_uint16()
        self.key = v_bytes()
        self.value_length = v_uint16()
        self.value = v_bytes()

    def pcb_key_length(self):
        self['key'].vsSetLength(self.key_length)

    def pcb_value_length(self):
        self['value'].vsSetLength(self.value_length)


class LeafEntryPointer(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.common_prefix = v_uint16()
        self.unk02 = v_uint16()
        self.offset = v_uint16()


class LeafEntry(vstruct.VStruct):
    def __init__(self, key, common_prefix):
        vstruct.VStruct.__init__(self)
        self.pkey = key
        self.common_prefix = common_prefix

        self.key_length = v_uint16()
        self._key = v_bytes()
        self.value_length = v_uint16()
        self.value = v_bytes()

        self.key = None

    def pcb_key_length(self):
        self['_key'].vsSetLength(self.key_length)

    def pcb_value_length(self):
        self['value'].vsSetLength(self.value_length)

    def pcb__key(self):
        self.key = self.pkey[:self.common_prefix] + self._key


class Page(vstruct.VStruct):
    '''
    single node in the b-tree.
    has a bunch of key-value entries that may point to other pages.
    binary search these keys and traverse pointers to efficienty query the index.

    branch node::

                                      +-------------+
        +-----------------------------+ ppointer    |  ----> [ node with keys less than entry1.key]
        | entry1.key | entry1.value   |-------------+
        +-----------------------------+ entry1.page |  ----> [ node with entry1.key < X < entry2.key]
        | entry2.key | entry2.value   |-------------+
        +-----------------------------+ entry2.page |  ----> [ node with entry2.key < X < entry3.key]
        | ...        | ...            |-------------+
        +-----------------------------+ ...         |
        | entryN.key | entryN.value   |-------------+
        +-----------------------------+ entryN.key  |  ----> [ node with keys greater than entryN.key]
                                      +-------------+

    leaf node::

        +-----------------------------+
        | entry1.key | entry1.value   |
        +-----------------------------+
        | entry2.key | entry2.value   |
        +-----------------------------+
        | ...        | ...            |
        +-----------------------------+
        | entryN.key | entryN.value   |
        +-----------------------------+

    '''
    def __init__(self, page_size):
        vstruct.VStruct.__init__(self)
        self.ppointer = v_uint32()
        self.entry_count = v_uint16()
        self.contents = v_bytes(page_size)
        # ordered cache of entries, once loaded.
        self._entries = []

    def is_leaf(self):
        '''
        return True if this is a leaf node.

        Returns:
          bool: True if this is a leaf node.
        '''
        return self.ppointer == 0

    def _load_entries(self):
        if not self._entries:
            key = b''
            for i in range(self.entry_count):
                if self.is_leaf():
                    ptr = LeafEntryPointer()
                    ptr.vsParse(self.contents, offset=i * SIZEOF_ENTRY)

                    entry = LeafEntry(key, ptr.common_prefix)
                    entry.vsParse(self.contents, offset=ptr.offset - SIZEOF_ENTRY)
                else:
                    ptr = BranchEntryPointer()
                    ptr.vsParse(self.contents, offset=i * SIZEOF_ENTRY)

                    entry = BranchEntry(int(ptr.page))
                    entry.vsParse(self.contents, offset=ptr.offset - SIZEOF_ENTRY)
                self._entries.append(entry)
                key = entry.key

    def get_entries(self):
        '''
        generate the entries from this page in order.
        each entry is guaranteed to have the following fields:
          - key
          - value

        Yields:
          Union[BranchEntry, LeafEntry]: the b-tree entries from this page.
        '''
        self._load_entries()
        for entry in self._entries:
            yield entry

    def get_entry(self, entry_number):
        '''
        get the entry at the given index.

        Arguments:
          entry_number (int): the entry index.

        Returns:
          Union[BranchEntry, LeafEntry]: the b-tree entry.

        Raises:
          KeyError: if the entry number is not in the range of entries.
        '''
        self._load_entries()
        if entry_number >= len(self._entries):
            raise KeyError(entry_number)
        return self._entries[entry_number]

    def validate(self):
        last = None
        for entry in self.get_entries():
            if last is None:
                continue

            if last.key >= entry.key:
                raise ValueError('bad page entry sort order')

            last = entry
        return True


class FindStrategy(object):
    '''
    defines the interface for strategies of searching the btree.

    implementors will provide a `.find()` method that operates on a `Cursor` instance.
    the method will update the cursor as it navigates the btree.
    '''
    __meta__ = abc.ABCMeta
    @abc.abstractmethod
    def find(self, cursor, key):
        raise NotImplementedError()


class ExactMatchStrategy(FindStrategy):
    '''
    strategy used to find the entry with exactly the key provided.
    if the exact key is not found, `KeyError` is raised.
    '''
    def _find(self, cursor, page_number, key):
        page = cursor.index.get_page(page_number)
        cursor.path.append(page)

        is_largest = False
        try:
            entry_number = cursor.find_index(page, key)
        except KeyError:
            # an entry larger than the given key is not found.
            # but we know we should be searching this node,
            #  so we must have to recurse into the final page pointer.
            is_largest = True
            entry_number = page.entry_count - 1

        entry = page.get_entry(entry_number)

        if entry.key == key:
            cursor.entry = entry
            cursor.entry_number = entry_number
            return
        elif page.is_leaf():
            # no matches!
            raise KeyError(key)
        else:
            if entry_number == 0:
                next_page_number = page.ppointer
            elif is_largest:
                next_page_number = page.get_entry(page.entry_count - 1).page
            else:
                next_page_number = page.get_entry(entry_number - 1).page
            self._find(cursor, next_page_number, key)
            return

    def find(self, cursor, key):
        self._find(cursor, cursor.index.root_page, key)


class PrefixMatchStrategy(FindStrategy):
    '''
    strategy used to find the first entry that begins with the given key.
    it may be an exact match, or an exact match does not exist, and the result starts with the given key.
    if no entries start with the given key, `KeyError` is raised.
    '''
    def _find(self, cursor, page_number, key):
        page = cursor.index.get_page(page_number)
        cursor.path.append(page)

        if page.is_leaf():
            for i, entry in enumerate(page.get_entries()):
                entry_key = bytes(entry.key)
                if entry_key.startswith(key):
                    cursor.entry = entry
                    cursor.entry_number = i
                    return
                elif entry_key > key:
                    # as soon as we reach greater entries, we'll never match
                    break
            raise KeyError(key)
        else:  # is branch node
            next_page = page.ppointer
            for i, entry in enumerate(page.get_entries()):
                entry_key = bytes(entry.key)
                if entry_key == key:
                    cursor.entry = entry
                    cursor.entry_number = i
                    return
                elif entry_key.startswith(key):
                    # the sub-page pointed to by this entry contains larger entries.
                    # so we need to look at the sub-page pointed to by the last entry (or ppointer).
                    return self._find(cursor, next_page, key)
                elif entry_key > key:
                    # as soon as we reach greater entries, we'll never match
                    break
                else:
                    next_page = entry.page

            # since we haven't found a matching entry, but we know our matches must be in the page,
            # we need to search the final sub-page, which contains the greatest entries.
            return self._find(cursor, next_page, key)

    def find(self, cursor, key):
        self._find(cursor, cursor.index.root_page, key)


# TODO: add MIN/MAX strategies?

EXACT_MATCH = ExactMatchStrategy
PREFIX_MATCH = PrefixMatchStrategy


class Cursor(object):
    '''
    represents a particular location in the b-tree.
    can be navigated "forward" and "backwards".
    '''
    def __init__(self, index):
        super(Cursor, self).__init__()
        self.index = index

        # ordered list of pages from root to leaf that we traversed to get to this point
        self.path = []

        # populated once found
        self.entry = None


        self.entry_number = None

    # TODO: consider moving this to the Page class.
    def find_index(self, page, key):
        '''
        find the index of the exact match, or in the case of a branch node,
         the index of the least-greater entry.
        '''
        # implementation note:
        #  suprisingly, using a binary search here does not substantially improve performance.
        #  this is probably the the dominating operations are parsing and allocating entries.
        #  the linear scan below is simpler to read, so we'll use that until it becomes an issue.
        if page.is_leaf():
            for i, entry in enumerate(page.get_entries()):
                # TODO: exact match only
                if key == entry.key:
                    return i
        else:
            for i, entry in enumerate(page.get_entries()):
                entry_key = bytes(entry.key)
                # TODO: exact match only
                if key == entry_key:
                    return i
                elif key < entry_key:
                    # this is the least-greater entry
                    return i
                else:
                    continue
        raise KeyError(key)

    def next(self):
        '''
        traverse to the next entry.
        updates this current cursor instance.

        Raises:
          IndexError: if the entry does not exist. the cursor is in an unknown state afterwards.
        '''
        current_page = self.path[-1]
        if current_page.is_leaf():
            if self.entry_number == current_page.entry_count - 1:
                # complex case: have to traverse up and then around.
                # we are at the end of a leaf node. so we need to go to the parent and find the next entry.
                # we may have to go up multiple parents.
                start_key = self.entry.key

                while True:
                    # pop the current node off the path
                    if len(self.path) <= 1:
                        raise IndexError()
                    self.path = self.path[:-1]

                    current_page = self.path[-1]
                    try:
                        entry_number = self.find_index(current_page, start_key)
                    except KeyError:
                        # not found, becaues its too big for this node.
                        # so we need to go higher.
                        continue
                    else:
                        # found a valid entry, so lets process it.
                        break

                # entry_number now points to the least-greater entry relative to start key.
                # this should be the entry that points to the page from which we just came.
                # we'll want to return the key from this entry.

                self.entry = current_page.get_entry(entry_number)
                self.entry_number = entry_number
                return

            else:  # is inner entry.
                # simple case: simply increment the entry number in the current node.
                next_entry_number = self.entry_number + 1
                next_entry = current_page.get_entry(next_entry_number)

                self.entry = next_entry
                self.entry_number = next_entry_number
                return
        else:  # is branch node.

            # follow the min-edge down to a leaf, and take the min entry.
            next_page = self.index.get_page(self.entry.page)
            while not next_page.is_leaf():
                self.path.append(next_page)
                next_page = self.index.get_page(next_page.ppointer)

            self.path.append(next_page)
            self.entry = next_page.get_entry(0)
            self.entry_number = 0
            return

    def prev(self):
        '''
        traverse to the previous entry.
        updates this current cursor instance.

        Raises:
          IndexError: if the entry does not exist. the cursor is in an unknown state afterwards.
        '''
        current_page = self.path[-1]
        if current_page.is_leaf():
            if self.entry_number == 0:
                # complex case: have to traverse up and then around.
                # we are at the beginning of a leaf node.
                # so we need to go to the parent and find the prev entry.
                # we may have to go up multiple parents.
                start_key = self.entry.key

                while True:
                    # pop the current node off the path
                    if len(self.path) <= 1:
                        raise IndexError()
                    self.path = self.path[:-1]

                    current_page = self.path[-1]
                    try:
                        entry_number = self.find_index(current_page, start_key)
                    except KeyError:
                        entry_number = current_page.entry_count

                    if entry_number == 0:
                        # not found, becaues its too small for this node.
                        # so we need to go higher.
                        continue
                    else:
                        break

                # entry_number now points to the least-greater entry relative to start key.
                # this should be the entry that points to the page from which we just came.
                # we'll want to return the key from the entry that is just smaller than this one.

                self.entry = current_page.get_entry(entry_number - 1)
                self.entry_number = entry_number - 1
                return

            else:  # is inner entry.
                # simple case: simply decrement the entry number in the current node.
                next_entry_number = self.entry_number - 1
                next_entry = current_page.get_entry(next_entry_number)

                self.entry = next_entry
                self.entry_number = next_entry_number
                return
        else:  # is branch node.

            # follow the max-edge down to a leaf, and take the max entry.
            current_page = self.path[-1]
            if self.entry_number == 0:
                next_page_number = current_page.ppointer
            else:
                next_page_number = current_page.get_entry(self.entry_number - 1).page

            next_page = self.index.get_page(next_page_number)
            while not next_page.is_leaf():
                self.path.append(next_page)
                next_page = self.index.get_page(next_page.get_entry(next_page.entry_count - 1).page)

            self.path.append(next_page)
            self.entry = next_page.get_entry(next_page.entry_count - 1)
            self.entry_number = next_page.entry_count - 1
            return

    @property
    def key(self):
        return self.entry.key

    @property
    def value(self):
        return self.entry.value


class ID0(vstruct.VStruct):
    '''
    a b-tree index.
    keys and values are arbitrary byte strings.

    use `.find()` to identify a matching entry, and use the resulting cursor
     instance to access the value, or traverse to less/greater entries.
    '''
    def __init__(self, buf):
        vstruct.VStruct.__init__(self)
        self.buf = memoryview(buf)

        self.next_free_offset = v_uint32()
        self.page_size = v_uint16()
        self.root_page = v_uint32()
        self.record_count = v_uint32()
        self.page_count = v_uint32()
        self.unk12 = v_uint8()
        self.signature = v_bytes(size=0x09)

    def get_page_buffer(self, page_number):
        if page_number < 1:
            logger.warning('unexpected page number requested: %d', page_number)
        offset = self.page_size * page_number
        return self.buf[offset:offset + self.page_size]

    def get_page(self, page_number):
        buf = self.get_page_buffer(page_number)
        page = Page(self.page_size)
        page.vsParse(buf)
        return page

    def find(self, key, strategy=EXACT_MATCH):
        '''
        Args:
          key (bytes): the index key for which to search.
          strategy (Type[MatchStrategy]): the strategy to use to do the search.
            some possible strategies:
              - EXACT_MATCH (default)
              - PREFIX_MATCH

        Returns:
          cursor: the cursor that points to the match.

        Raises:
          KeyError: if the match failes to find a result.
        '''
        c = Cursor(self)
        s = strategy()
        s.find(c, key)
        return c

    def find_prefix(self, key):
        '''
        convenience shortcut for prefix match search.
        '''
        return self.find(key, strategy=PREFIX_MATCH)

    def validate(self):
        if self.signature != b'B-tree v2':
            raise ValueError('bad signature')
        return True


class SegmentBounds(vstruct.VStruct):
    '''
    specifies the range of a segment.
    '''
    def __init__(self, wordsize=4):
        vstruct.VStruct.__init__(self)

        self.wordsize = wordsize
        if wordsize == 4:
            self.v_word = v_uint32
            self.word_fmt = "I"
        elif wordsize == 8:
            self.v_word = v_uint64
            self.word_fmt = "Q"
        else:
            raise RuntimeError('unexpected wordsize')

        self.start = self.v_word()
        self.end = self.v_word()


class ID1(vstruct.VStruct):
    '''
    contains flags for each byte.
    '''
    PAGE_SIZE = 0x2000

    def __init__(self, wordsize=4, buf=None):
        vstruct.VStruct.__init__(self)

        self.wordsize = wordsize
        if wordsize == 4:
            self.v_word = v_uint32
            self.word_fmt = "I"
        elif wordsize == 8:
            self.v_word = v_uint64
            self.word_fmt = "Q"
        else:
            raise RuntimeError('unexpected wordsize')

        self.signature = v_bytes(size=0x04)
        self.unk04 = v_uint32()     # 0x3
        self.segment_count = v_uint32()
        self.unk0C = v_uint32()     # 0x800
        self.page_count = v_uint32()
        # varrays are not actually very list-like,
        #  so the struct field will be ._segments
        #  and the property will be .segments.
        self._segments = vstruct.VArray()
        self.segments = []
        self.padding = v_bytes()
        self.buffer = v_bytes()

    SegmentDescriptor = namedtuple('SegmentDescriptor', ['bounds', 'offset'])

    def pcb_segment_count(self):
        # TODO: pass wordsize
        self['_segments'].vsAddElements(self.segment_count, SegmentBounds)
        offset = 0
        for i in range(self.segment_count):
            segment = self._segments[i]
            offset += 4 * (segment.end - segment.start)
            self.segments.append(ID1.SegmentDescriptor(segment, offset))
        offset = 0x14 + (self.segment_count * (2 * self.wordsize))
        padsize = ID1.PAGE_SIZE - offset
        self['padding'].vsSetLength(padsize)

    def pcb_page_count(self):
        self['buffer'].vsSetLength(ID1.PAGE_SIZE * self.page_count)

    def get_segment(self, ea):
        '''
        find the segment that contains the given effective address.

        Returns:
          SegmentDescriptor: segment metadata and location.

        Raises:
          KeyError: if the given address is not in a segment.
        '''
        for segment in self.segments:
            if segment.bounds.start <= ea < segment.bounds.end:
                return segment
        raise KeyError(ea)

    def get_next_segment(self, ea):
        '''
        Fetch the next segment.

        Arguments:
          ea (int): an effective address that should fall within a segment.

        Returns:
          int: the effective address of the start of a segment.

        Raises:
          IndexError: if no more segments are found after the given segment.
          KeyError: if the given effective address does not fall within a segment.
        '''
        for i, segment in enumerate(self.segments):
            if segment.bounds.start <= ea < segment.bounds.end:
                if i == len(self.segments):
                    # this is the last segment, there are no more.
                    raise IndexError(ea)
                else:
                    # there's at least one more, and that's the next one.
                    return self.segments[i + 1]
        raise KeyError(ea)

    def get_flags(self, ea):
        '''
        Fetch the flags for the given effective address.

        > Each byte of the program has 32-bit flags (low 8 bits keep the byte value).
        > These 32 bits are used in GetFlags/SetFlags functions.
        via: https://www.hex-rays.com/products/ida/support/idapython_docs/idc-module.html

        Arguments:
          ea (int): the effective address.

        Returns:
          int: the flags for the given address.

        Raises:
          KeyError: if the given address does not fall within a segment.
        '''
        seg = self.get_segment(ea)
        offset = seg.offset + 4 * (ea - seg.bounds.start)
        return struct.unpack_from('<I', self.buffer, offset)[0]

    def validate(self):
        if self.signature != b'VA*\x00':
            raise ValueError('bad signature')
        if self.unk04 != 0x3:
            raise ValueError('unexpected unk04 value')
        if self.unk0C != 0x800:
            raise ValueError('unexpected unk0C value')
        for segment in self.segments:
            if segment.bounds.start > segment.bounds.end:
                raise ValueError('segment ends before it starts')
        return True


class NAM(vstruct.VStruct):
    '''
    contains pointers to named items.
    '''
    PAGE_SIZE = 0x2000

    def __init__(self, wordsize=4, buf=None):
        vstruct.VStruct.__init__(self)

        self.wordsize = wordsize
        if wordsize == 4:
            self.v_word = v_uint32
            self.word_fmt = "I"
        elif wordsize == 8:
            self.v_word = v_uint64
            self.word_fmt = "Q"
        else:
            raise RuntimeError('unexpected wordsize')

        self.signature = v_bytes(size=0x04)
        self.unk04 = v_uint32()      # 0x3
        self.non_empty = v_uint32()  # (0x1 non-empty) or (0x0 empty)
        self.unk0C = v_uint32()      # 0x800
        self.page_count = v_uint32()
        self.unk14 = self.v_word()   # 0x0
        self.name_count = v_uint32()
        self.padding = v_bytes(size=NAM.PAGE_SIZE - (6 * 4 + wordsize))
        self.buffer = v_bytes()

    def pcb_page_count(self):
        self['buffer'].vsSetLength(self.page_count * NAM.PAGE_SIZE)

    def validate(self):
        if self.signature != b'VA*\x00':
            raise ValueError('bad signature')
        if self.unk04 != 0x3:
            raise ValueError('unexpected unk04 value')
        if self.non_empty not in (0x0, 0x1):
            raise ValueError('unexpected non_empty value')
        if self.unk0C != 0x800:
            raise ValueError('unexpected unk0C value')
        if self.unk14 != 0x0:
            raise ValueError('unexpected unk14 value')
        return True

    def names(self):
        fmt = "<{0.name_count:d}{0.word_fmt:s}".format(self)
        size = struct.calcsize(fmt)
        if size > len(self.buffer):
            raise ValueError('buffer too small')
        return struct.unpack(fmt, self.buffer[:size])


class TIL(vstruct.VStruct):
    def __init__(self, buf=None):
        vstruct.VStruct.__init__(self)
        self.signature = v_bytes(size=0x06)

    def validate(self):
        if self.signature != b'IDATIL':
            raise ValueError('bad signature')
        return True


SectionDescriptor = namedtuple('SectionDescriptor', ['name', 'cls'])

# section order:
#   - id0
#   - id1
#   - nam
#   - seg
#   - til
#   - id2
#
# via: https://github.com/williballenthin/pyidbutil/blob/master/idblib.py#L262
SECTIONS = [
    SectionDescriptor('id0', ID0),
    SectionDescriptor('id1', ID1),
    SectionDescriptor('nam', NAM),
    SectionDescriptor('seg', None),
    SectionDescriptor('til', TIL),
    SectionDescriptor('id2', None),
]


class IDB(vstruct.VStruct):
    def __init__(self, buf):
        vstruct.VStruct.__init__(self)
        # we use a memoryview since we'll take a bunch of read-only subslices.
        self.buf = memoryview(buf)

        # list of parsed Section instances or None.
        # the entries should line up with the SECTIONS definition.
        self.sections = []

        # these fields will be parsed from self.buf once the header is parsed.
        # they are *not* linearly parsed during .vsParse().
        self.id0 = None  # type: ID0
        self.id1 = None  # type: ID1
        self.nam = None  # type: NAM
        self.seg = None  # type: NotImplemented
        self.til = None  # type: TIL
        self.id2 = None  # type: NotImplemented

        # these are the only true vstruct fields for this struct.
        self.header = FileHeader()

        # TODO: set this correctly.
        # possibly inspect the magic header?
        self.wordsize = 4

    def pcb_header(self):
        # TODO: pass along checksum
        for offset in self.header.offsets:
            if offset == 0:
                self.sections.append(None)
                continue

            sectionbuf = self.buf[offset:]
            section = Section()
            section.vsParse(sectionbuf)
            self.sections.append(section)

        for i, sectiondef in enumerate(SECTIONS):
            if i > len(self.sections):
                logger.debug('missing section: %s', sectiondef.name)
                continue

            section = self.sections[i]
            if not section:
                logger.debug('missing section: %s', sectiondef.name)
                continue

            if not sectiondef.cls:
                logger.warn('section class not implemented: %s', sectiondef.name)
                continue

            s = sectiondef.cls(buf=section.contents)
            s.vsParse(section.contents)
            # vivisect doesn't allow you to assign vstructs to
            #  attributes that are not part of the struct,
            # so we need to override and use the default object behavior.
            object.__setattr__(self, sectiondef.name, s)
            logger.debug('parsed section: %s', sectiondef.name)

    def validate(self):
        self.header.validate()
        self.id0.validate()
        self.id1.validate()
        self.nam.validate()
        self.til.validate()
        return True

    def netnode(self, *args, **kwargs):
        return idb.netnode.Netnode(self, *args, **kwargs)

    def SegStart(self, ea):
        # TODO: i think this should use '$ fileregions'
        return self.id1.get_segment(ea).bounds.start

    def SegEnd(self, ea):
        # TODO: i think this should use '$ fileregions'
        return self.id1.get_segment(ea).bounds.end

    def FirstSeg(self):
        # TODO: i think this should use '$ fileregions'
        return self.id1.segments[0].bounds.start

    def NextSeg(self, ea):
        # TODO: i think this should use '$ fileregions'
        return self.id1.get_next_segment(ea).bounds.start

    def GetFlags(self, ea):
        return self.id1.get_flags(ea)

    def IdbByte(self, ea):
        flags = self.GetFlags(ea)
        if self.hasValue(flags):
            return flags & FLAGS.MS_VAL
        else:
            raise KeyError(ea)

    def Head(self, ea):
        flags = self.GetFlags(ea)
        while not self.isHead(flags):
            ea -= 1
            # TODO: handle Index/KeyError here when we overrun a segment
            flags = self.GetFlags(ea)
        return ea

    def NextHead(self, ea):
        ea += 1
        flags = self.GetFlags(ea)
        while not self.isHead(flags):
            ea += 1
            # TODO: handle Index/KeyError here when we overrun a segment
            flags = self.GetFlags(ea)
        return ea

    def PrevHead(self, ea):
        ea = self.Head(ea)
        ea -= 1
        return self.Head(ea)

    def GetManyBytes(self, ea, size, use_dbg=False):
        '''
        Raises:
          IndexError: if the range extends beyond a segment.
          KeyError: if a byte is not defined.
        '''
        if use_dbg:
            raise NotImplementedError()

        if self.SegStart(ea) != self.SegStart(ea + size):
            raise IndexError((ea, ea+size))

        ret = []
        for i in range(ea, ea + size):
            ret.append(self.IdbByte(i))
        return bytes(ret)

    def hasValue(self, flags):
        return flags & FLAGS.FF_IVL > 0

    def isFunc(self, flags):
        return flags & FLAGS.MS_CODE == FLAGS.FF_FUNC

    def isImmd(self, flags):
        return flags & FLAGS.MS_CODE == FLAGS.FF_IMMD

    def isCode(self, flags):
        return flags & FLAGS.MS_CLS == FLAGS.FF_CODE

    def isData(self, flags):
        return flags & FLAGS.MS_CLS == FLAGS.FF_DATA

    def isTail(self, flags):
        return flags & FLAGS.MS_CLS == FLAGS.FF_TAIL

    def isNotTail(self, flags):
        return not self.isTail(flags)

    def isUnknown(self, flags):
        return flags & FLAGS.MS_CLS == FLAGS.FF_UNK

    def isHead(self, flags):
        return self.isCode(flags) or self.isData(flags)

    def isFlow(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_FLOW > 0

    def isVar(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_VAR > 0

    def hasExtra(self, flags):
        print(flags & FLAGS.FF_LINE)
        return flags & FLAGS.MS_COMM & FLAGS.FF_LINE > 0

    def has_cmt(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_COMM > 0

    def hasRef(self, flags):
        print(flags & FLAGS.FF_REF)
        return flags & FLAGS.MS_COMM & FLAGS.FF_REF > 0

    def has_name(self, flags):
        print(flags & FLAGS.FF_NAME)
        return flags & FLAGS.MS_COMM & FLAGS.FF_NAME > 0

    def has_dummy_name(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_LABL > 0

    def has_auto_name(self, flags):
        # unknown how to compute this
        raise NotImplementedError()

    def has_any_name(self, flags):
        # unknown how to compute this
        raise NotImplementedError()

    def has_user_name(self, flags):
        # unknown how to compute this
        raise NotImplementedError()

    def is_invsign(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_SIGN > 0

    def is_bnot(self, flags):
        return flags & FLAGS.MS_COMM & FLAGS.FF_BNOT > 0

    def isByte (self, flags):
        return flags & FLAGS.DT_TYPE ==	FLAGS.FF_BYTE

    def isWord (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_WORD

    def isDwrd (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_DWRD

    def isQwrd (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_QWRD

    def isOwrd (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_OWRD

    def isYwrd (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_YWRD

    def isTbyt (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_TBYT

    def isFloat (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_FLOAT

    def isDouble (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_DOUBLE

    def isPackReal (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_PACKREAL

    def isASCII (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_ASCI

    def isStruct (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_STRU

    def isAlign (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_ALIGN

    def is3byte (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_3BYTE

    def isCustom (self, flags):
 	      return flags & FLAGS.DT_TYPE ==	FLAGS.FF_CUSTOM

    def isDefArg0(self, flags):
        '''
        Is the first operand defined? Initially operand has no defined representation.
        '''
        return flags & FLAGS.MS_0TYPE > 0

    def isDefArg1(self, flags):
        '''
        Is the second operand defined? Initially operand has no defined representation.
        '''
        return flags & FLAGS.MS_1TYPE > 0

    def isOff0(self, flags):
        '''
        Is the first operand offset? (example: push offset xxx)
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0CUST

    def isOff1(self, flags):
        '''
        Is the second operand offset? (example: mov ax, offset xxx)
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1CUST

    def isChar0(self, flags):
        '''
        Is the first operand character constant? (example: push 'a')
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0CHAR

    def isChar1(self, flags):
        '''
        Is the second operand character constant? (example: mov al, 'a')
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1CHAR

    def isSeg0(self, flags):
        '''
        Is the first operand segment selector? (example: push seg seg001)
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0SEG

    def isSeg1(self, flags):
        '''
        Is the second operand segment selector? (example: mov dx, seg dseg)
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1SEG

    def isEnum0(self, flags):
        '''
        Is the first operand a symbolic constant (enum member)?
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0ENUM

    def isEnum1(self, flags):
        '''
        Is the second operand a symbolic constant (enum member)?
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1ENUM

    def isStroff0(self, flags):
        '''
        Is the first operand an offset within a struct?
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0STRO

    def isStroff1(self, flags):
        '''
        Is the second operand an offset within a struct?
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1STRO

    def isStkvar0(self, flags):
        '''
        Is the first operand a stack variable?
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0STK

    def isStkvar1(self, flags):
        '''
        Is the second operand a stack variable?
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1STK

    def isFloat0(self, flags):
        '''
        Is the first operand a floating point number?
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0FLT

    def isFloat1(self, flags):
        '''
        Is the second operand a floating point number?
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1FLT

    def isCustFmt0(self, flags):
        '''
        Does the first operand use a custom data representation?
        '''
        return flags & FLAGS.MS_0TYPE == FLAGS.FF_0CUST

    def isCustFmt1(self, flags):
        '''
        Does the second operand use a custom data representation?
        '''
        return flags & FLAGS.MS_1TYPE == FLAGS.FF_1CUST

    def isNum0(self, flags):
        '''
        Is the first operand a number (i.e. binary,octal,decimal or hex?)
        '''
        t = flags & FLAGS.MS_0TYPE
        return t == FLAGS.FF_0NUMB or \
               t == FLAGS.FF_0NUMO or \
               t == FLAGS.FF_0NUMD or \
               t == FLAGS.FF_0NUMH

    def isNum1(self, flags):
        '''
        Is the second operand a number (i.e. binary,octal,decimal or hex?)
        '''
        t = flags & FLAGS.MS_1TYPE
        return t == FLAGS.FF_1NUMB or \
               t == FLAGS.FF_1NUMO or \
               t == FLAGS.FF_1NUMD or \
               t == FLAGS.FF_1NUMH

    def get_optype_flags0(self, flags):
        '''
        Get flags for first operand.
        '''
        return flags & FLAGS.MS_0TYPE

    def get_optype_flags1(self, flags):
        '''
        Get flags for second operand.
        '''
        return flags & FLAGS.MS_1TYPE

    # TODO: methods here: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__opfuncs2.html


class FLAGS:
    # instruction/data operands
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__op.html

    # outer offset base (combined with operand number). More...
    OPND_OUTER = 0x80

    # mask for operand number
    OPND_MASK = 0x07

    # all operands
    OPND_ALL = OPND_MASK

    # byte states bits
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__statebits.html

 	  # Mask for typing.
    MS_CLS = 0x00000600

    # Code ?
    FF_CODE = 0x00000600

    # Data ?
    FF_DATA = 0x00000400

 	  # Tail ?
    FF_TAIL = 0x00000200

    # Unknown ?
    FF_UNK = 0x00000000

    # specific state information bits
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__statespecb.html

    # Mask of common bits.
    MS_COMM = 0x000FF800

    # Has comment ?
    FF_COMM = 0x00000800

    # has references
    FF_REF = 0x00001000

    # Has next or prev lines ?
    FF_LINE = 0x00002000

    # Has name ?
    FF_NAME = 0x00004000

    # Has dummy name?
    FF_LABL = 0x00008000

    # Exec flow from prev instruction.
    FF_FLOW = 0x00010000

    # Inverted sign of operands.
    FF_SIGN = 0x00020000

    # Bitwise negation of operands.
    FF_BNOT = 0x00040000

    # is variable byte?
    FF_VAR = 0x00080000

    # instruction operand types bites
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__opbits.html

    # Mask for 1st arg typing.
    MS_0TYPE = 0x00F00000

    # Void (unknown)?
    FF_0VOID = 0x00000000

    # Hexadecimal number?
    FF_0NUMH = 0x00100000

    # Decimal number?
    FF_0NUMD = 0x00200000

    # Char ('x')?
    FF_0CHAR = 0x00300000

    # Segment?
    FF_0SEG = 0x00400000

    # Offset?
    FF_0OFF = 0x00500000

    # Binary number?
    FF_0NUMB = 0x00600000

    # Octal number?
    FF_0NUMO = 0x00700000

    # Enumeration?
    FF_0ENUM = 0x00800000

    # Forced operand?
    FF_0FOP = 0x00900000

    # Struct offset?
    FF_0STRO = 0x00A00000

    # Stack variable?
    FF_0STK = 0x00B00000

    # Floating point number?
    FF_0FLT = 0x00C00000

    # Custom representation?
    FF_0CUST = 0x00D00000

    # Mask for the type of other operands.
    MS_1TYPE = 0x0F000000

    # Void (unknown)?
    FF_1VOID = 0x00000000

    # Hexadecimal number?
    FF_1NUMH = 0x01000000

    # Decimal number?
    FF_1NUMD = 0x02000000

    # Char ('x')?
    FF_1CHAR = 0x03000000

    # Segment?
    FF_1SEG = 0x04000000

    # Offset?
    FF_1OFF = 0x05000000

    # Binary number?
    FF_1NUMB = 0x06000000

    # Octal number?
    FF_1NUMO = 0x07000000

    # Enumeration?
    FF_1ENUM = 0x08000000

    # Forced operand?
    FF_1FOP = 0x09000000

    # Struct offset?
    FF_1STRO = 0x0A000000

    # Stack variable?
    FF_1STK = 0x0B000000

    # Floating point number?
    FF_1FLT = 0x0C000000

    # Custom representation?
    FF_1CUST = 0x0D000000

    # code byte bits
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__codebits.html
 	  # Mask for code bits.
    MS_CODE = 0xF0000000

 	  # function start?
    FF_FUNC = 0x10000000

    # Has Immediate value?
    FF_IMMD = 0x40000000

 	  # Has jump table or switch_info?
    FF_JUMP = 0x80000000

    # data bytes bits
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__databits.html

    # Mask for DATA typing.
    DT_TYPE = 0xF0000000

    # byte
    FF_BYTE = 0x00000000

    # word
    FF_WORD = 0x10000000

    # double word
    FF_DWRD = 0x20000000

    # quadro word
    FF_QWRD = 0x30000000

    # tbyte
    FF_TBYT = 0x40000000

    # ASCII ?
    FF_ASCI = 0x50000000

    # Struct ?
    FF_STRU = 0x60000000

    # octaword/xmm word (16 bytes/128 bits)
    FF_OWRD = 0x70000000

    # float
    FF_FLOAT = 0x80000000

    # double
    FF_DOUBLE = 0x90000000

    # packed decimal real
    FF_PACKREAL = 0xA0000000

    # alignment directive
    FF_ALIGN = 0xB0000000

    # 3-byte data (only with support from the processor module)
    FF_3BYTE = 0xC0000000

    # custom data type
    FF_CUSTOM = 0xD0000000

    # ymm word (32 bytes/256 bits)
    FF_YWRD = 0xE0000000

    # bytes
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___f_f__.html

    # Mask for byte value.
    MS_VAL = 0x000000FF

    # Byte has value?
    FF_IVL = 0x00000100
