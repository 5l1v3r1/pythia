import io
import logging
import struct
import pefile
from binascii import hexlify
from .helpers import *
from .structures import *
from .objects import *
from .utils import unpack_stream


class PEHandler(object):
    """
    The main parser, which takes a filename or pefile object and extracts
    information.  If pythia is updated to parse other file types then
    much of the code will need splitting out into a separate class.
    """

    # TODO: Support callbacks, which will allow other programs (idapython,
    #       radare) to use this programatically.  Ideally these should be
    #       passed to the higher level class.

    # TODO: Support parsing a single section, if given the data and the
    #       base virtual address.  This will permit usage from within
    #       IDA.

    _pe = None

    def __init__(self, logger, context, filename=None, pe=None):
        # TODO: Create our own logger
        self.logger = logger
        self.context = context

        if filename:
            self._from_file(filename)

        elif pe:
            self._from_pefile(pe)

    def _from_pefile(self, pe):
        """
        Initialise with an existing pefile object, useful when some other
        script has already created the object.
        """
        self._pe = pe
        self._pehelper = PEHelper(pe)
        self._mapped_data = self._pe.get_memory_mapped_image()

        # TODO: Validate 32bit.  Need to find 64bit samples to add parsing.
        self._extract_access_license(pe)
        self._extract_packageinfo(pe)

        self.logger.debug(
            "ImageBase is: 0x{:08x}".format(self._pe.OPTIONAL_HEADER.ImageBase)
        )

    def _from_file(self, filename):
        """
        Initialise from a file on disk.
        """

        # TODO: Exception handling - test with junk data
        pe = pefile.PE(filename, fast_load=True)
        self.logger.info("Loading PE from file {}".format(filename))
        self._from_pefile(pe)

    def _extract_access_license(self, pe):
        """
        Extract information from the DVCLAL resource.
        """

        helper = LicenseHelper()
        resource_type = pefile.RESOURCE_TYPE["RT_RCDATA"]
        data = self._pehelper.get_resource_data(resource_type, "DVCLAL")

        if data:
            license = helper.from_bytes(data)
            if license:
                self.logger.info(
                    "Found Delphi %s license information in PE resources", license
                )
                self.context.license = license
            else:
                self.logger.warning(
                    "Unknown Delphi license %s", hexlify(license)
                )

        else:
            self.logger.warning(
                "Did not find DVCLAL license information in PE resources"
            )

    def _extract_packageinfo(self, pe):
        """
        Extract information about what units this executable contains.
        """

        helper = PackageInfoHelper()
        resource_type = pefile.RESOURCE_TYPE["RT_RCDATA"]
        data = self._pehelper.get_resource_data(resource_type, "PACKAGEINFO")

        if data:
            # TODO: Get the output and do something with it
            helper.from_bytes(data)
            self.context.units = helper

        else:
            self.logger.warning(
                "Did not find PACKAGEINFO DVCLAL license information in PE resources"
            )

    def analyse(self):

        # TODO: This is incompatible with an API for IDA / Ghidra that takes data from
        #       one section.

        self.work_queue = WorkQueue()

        units = UnitInitHelper(self._pehelper)
        table_pos = units.find_init_table()
        if table_pos:
            self.logger.debug("Unit initialisation table is at 0x{:08x}".format(table_pos))
        else:
            self.logger.warning("Unit initialisation table not found, cannot continue")

        # There may be multiple code sections.  Find the one containing the unit initialisation
        # table and process it.  Multiple sections containing Delphi objects are not currently
        # supported, and it is unknown whether this would be generated by the Delphi compiler.
        sections = self._find_code_sections()
        section_names = ", ".join(s.name for s in sections)
        self.logger.debug("Found {} code section(s) named: {}".format(len(sections), section_names))
        code_section = None

        self.context.code_sections = sections
        self.context.data_sections = self._find_data_sections()

        for s in sections:
            if s.contains_va(s.load_address):
                self.logger.debug("Unit initialisation table should be in section {}".format(s.name))
                code_section = s
                break

        if code_section is None:
            self.logger.error("Could not find code section containing the entry point (whilst looking for unit initialisation table), cannot continue")
            return

        self.logger.info("Analysing section {}".format(s.name))

        if table_pos:
            init_table = units.parse_init_table(code_section, table_pos, self.context)

        # Manually scan the main code section and try to guess the length of
        # the vftable header.  This relies on finding TObject and ensuring
        # headers are expected values, from which we can derive the "distance"
        # measure (effectively the length of a standard vftable header).
        #
        # Note that if this fails it's a critical problem, because it means
        # we have not found TObject (either it's not object oriented, or the
        # executable is modified in some way e.g. packed).
        self.logger.info("Step 1: finding code section containing TObject")
        found = False
        for s in self.context.code_sections:
            result = self._find_tobject(s)
            if result:
                self.logger.info("Found TObject in section {}, standard vftable length is {}".format(s.name, result["vftable_length"]))

                if found:
                    self.logger.error("Found TObject in multiple sections, this is unusual and may cause parsing errors.  Please report as a bug.")

                    # FIXME: Find an example file to trigger this & test improvements.
                    #        Github issue #3.
                    raise Exception("Objects in more than one section")

                found = True
                self.context.header_length = result["vftable_length"]
                self.context.object_section = s

        if not found:
            self.logger.error("Did not find TObject in any code section")
            return

        # Step 2 - hunt for vftables
        self.logger.info("Step 2: finding potential vftable locations")
        vftables = self._find_vftables(self.context)

        # Step 3 - parse only vftables
        self.logger.info("Step 3: parsing vftables")
        # TODO: Remove duplicated code
        item = self.work_queue.get_item(obj_type=Vftable)
        while item:
            try:
                obj = item["item_type"](code_section, item["location"], self.context, work_queue=self.work_queue)
                self.context.items.append(obj)
                self.logger.debug(obj)

            except ValidationError:
                pass

            item = self.work_queue.get_item(obj_type=Vftable)

        # Step 4 - determine Delphi compiler version
        # We need an accurate version before further parsing, some of the RTTI
        # objects change as the compiler has evolved.
        self.logger.info("Step 4: determining likely Delphi compiler version")
        self.context.version = VersionHelper(self.context)

        self.logger.info("Step 5: parsing other RTTI objects")
        item = self.work_queue.get_item()
        while item:

            try:
                self.logger.debug("Work queue is processing object of type {} at 0x{:08x}".format(item["item_type"], item["location"]))
                obj = item["item_type"](code_section, item["location"], self.context, work_queue=self.work_queue)
                self.context.items.append(obj)
                self.logger.debug(obj)

            except ValidationError:
                # This is fine for Vftables found during the manual scan (as there may be
                # false positives) but should not normally happen otherwise.
                self.logger.debug("Could not validate object type {} at {:08x}".format(item["item_type"], item["location"]))

            item = self.work_queue.get_item()

        self.logger.debug(self.work_queue._queue)

        # TODO: Ensure the top class is always TObject, or warn
        # TODO: In strict mode, ensure no found items overlap (offset + length)
        # TODO: Check all parent classes have been found during the automated scan
        # TODO: Build up a hierarchy of classes

    def _find_sections(self, flags=None):
        sections = []

        # Check each code segment to see if it has the code flag
        for section in self._pe.sections:
            if flags and section.Characteristics & flags:
                sections.append(PESection(section, self._mapped_data))

        return sections

    def _find_code_sections(self):
        """
        Obtain a list of PESection objects for code sections.
        """
        return self._find_sections(pefile.SECTION_CHARACTERISTICS["IMAGE_SCN_CNT_CODE"])

    def _find_data_sections(self):
        """
        Obtain a list of PESection objects for code sections.
        """
        return self._find_sections(pefile.SECTION_CHARACTERISTICS["IMAGE_SCN_CNT_INITIALIZED_DATA"])


    def _find_tobject(self, code_section):
        """
        Scan the code section looking for TObject, then attempt to automatically
        determine the standard size of the vftable.  This size will be 11 DWORD
        headers plus pointers for each object function.

        This is required for the later vftable scan and also assists with version
        detection.
        """

        confirmed_distance = None
        i = 0

        while i < code_section.size - 128:
            # Save i and increment it, allows use of continue
            j = i
            i += 4

            # First look for the self pointer, which points to the end of the
            # standard vftable (either the first class function or the end of
            # vftable if there are no methods).
            code_section.stream_data.seek(j)
            (ptr, check1, check2) = unpack_stream("III", code_section.stream_data)

            # Quickly check the interface and auto table are set to 0, which
            # is consistent for TObject across Delphi versions.
            if check1 != 0 or check2 != 0:
                continue

            # Check it's within this section, or we should stop.
            if not code_section.contains_va(ptr):
                continue

            # Calculate the header length, which is the difference between the
            # pointer value and where we are currently at.
            offset = code_section.offset_from_va(ptr)
            difference = offset - j

            # Check for sensible lower and upper bounds
            if difference < 36 or difference > 128:
                continue

            # Extract the potential name pointer header, which is the 9th header.
            code_section.stream_data.seek(j + 32)
            (name_ptr, ) = unpack_stream("I", code_section.stream_data)

            # Check the pointer is within this section
            if not code_section.contains_va(name_ptr):
                continue

            # Extract the name
            code_section.stream_data.seek(code_section.offset_from_va(name_ptr))
            (name, ) = unpack_stream("I", code_section.stream_data)

            # This is \x07TObj, the start of the name field
            if name == 0x624F5407:
                # TODO: Check 7 empty DWORDs
                confirmed_distance = difference
                break

        if confirmed_distance:
            return { "va": code_section.va_from_offset(j), "vftable_length": confirmed_distance }

        return None


    def _find_vftables(self, context):
        """
        """
        i = 0
        found = 0
        section = context.object_section

        while i < section.size - context.header_length:
            section.stream_data.seek(i)
            (ptr, ) = unpack_stream("I", section.stream_data)

            # Calculate the virtual address of this location
            va = section.load_address + i
            # TODO: Enable when better logging granularity is available
            #self.logger.debug("i is {} and VA 0x{:08x} points to 0x{:08x}".format(i, va, ptr))

            if (va + context.header_length) == ptr:

                # Validate the first five DWORDs.  Regardless of Delphi version these
                # should be 0 (not set) or a pointer within this section.  This helps to
                # reduce the number of false positives we add to the work queue.
                #
                # A more thorough check is conducted when parsing this into an object later,
                # but this simple test useful.
                j = 5
                error = False

                while j:
                    (ptr, ) = unpack_stream("I", section.stream_data)
                    if ptr != 0 and not section.contains_va(ptr):
                        error = True
                    j -= 1

                if not error:
                    found += 1
                    #self.logger.debug("Found a potential vftable at 0x{:08x}".format(va))
                    self.work_queue.add_item(va, Vftable)

            # FIXME: 32-bit assumption, see Github issue #6
            i += 4

        return found
