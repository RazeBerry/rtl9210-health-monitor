/*
 * rtl9210_health_probe.m
 *
 * Read-only macOS transport for NVMe health data behind a Realtek RTL9210.
 *
 * The bridge exposes BOT (alternate setting 0, protocol 0x50) and UAS
 * (alternate setting 1, protocol 0x62) on interface 0. macOS normally binds
 * IOUSBMassStorageUASDriver. Device capture alone is insufficient because the
 * interface driver can re-bind. The working sequence is:
 *
 *   capture device
 *   configureWithValue:0 matchInterfaces:NO
 *   configureWithValue:1 matchInterfaces:NO
 *   open interface 0 and select BOT alternate setting 0
 *   issue read-only Realtek 0xE4 data-in commands
 *
 * Only SCSI INQUIRY, NVMe Identify Controller, and NVMe Get Log Page 0x02/0x01
 * are implemented. Use the rtl9210-health front door; do not invoke this
 * executable directly.
 */

#import <Foundation/Foundation.h>
#import <IOUSBHost/IOUSBHost.h>

#include <errno.h>
#include <inttypes.h>
#include <unistd.h>
#include "cancellation.h"

#pragma pack(push, 1)
typedef struct {
    uint32_t sig, tag, data_len;
    uint8_t flags, lun, cb_len;
    uint8_t cdb[16];
} cbw_t;

typedef struct {
    uint32_t sig, tag, residue;
    uint8_t status;
} csw_t;
#pragma pack(pop)

typedef struct {
    uint16_t vid;
    uint16_t pid;
    uint16_t bcd;
    char usb_serial[128];
    char nvme_model[41];
    char nvme_serial[21];
} identity_t;

/* Validate the complete configuration before changing USB configuration.
 * This adapter deliberately supports only the reviewed interface-0 BOT layout.
 * No endpoint guesses are made for other firmware layouts. */
static bool validate_bot_layout(const IOUSBConfigurationDescriptor *config)
{
    if (!config) return false;
    const uint8_t *b = (const uint8_t *)config;
    size_t total = (size_t)b[2] | ((size_t)b[3] << 8);
    if (b[0] < 9 || b[1] != 2 || total < b[0] || total > 4096 || b[5] != 1)
        return false;
    bool bot = false, in = false, out = false;
    unsigned matches = 0, endpoints = 0;
    for (size_t pos = b[0]; pos < total;) {
        if (total - pos < 2 || b[pos] < 2 || b[pos] > total - pos) return false;
        const uint8_t *d = b + pos;
        if (d[1] == 4) {
            if (bot && (!in || !out || endpoints != 2)) return false;
            if (d[0] < 9) return false;
            bot = d[2] == 0 && d[3] == 0;
            if (bot) {
                if (++matches != 1 || d[4] != 2 || d[5] != 8 ||
                    d[6] != 6 || d[7] != 0x50) return false;
                in = out = false;
                endpoints = 0;
            }
        } else if (d[1] == 5 && bot) {
            if (d[0] < 7 || (d[3] & 3) != 2 || !(d[4] | d[5])) return false;
            endpoints++;
            if (d[2] == 0x81 && !in) in = true;
            else if (d[2] == 0x02 && !out) out = true;
            else return false;
        }
        pos += d[0];
    }
    return matches == 1 && in && out && endpoints == 2;
}

static IOUSBHostInterface *g_intf = nil;
static IOUSBHostPipe *g_in = nil;
static IOUSBHostPipe *g_out = nil;
static uint32_t g_tag = 0x554e4300; /* 'UNC\0' */

static int bot_command_in(const uint8_t *cdb, uint8_t cdb_len, uint8_t *buf,
                          uint32_t data_len)
{
    if (rtl_cancel_signal) return -6;
    cbw_t cbw;
    memset(&cbw, 0, sizeof cbw);
    cbw.sig = 0x43425355u;
    cbw.tag = ++g_tag;
    cbw.data_len = data_len;
    cbw.flags = 0x80;
    cbw.lun = 0;
    cbw.cb_len = cdb_len;
    memcpy(cbw.cdb, cdb, cdb_len);

    NSError *err = nil;
    NSUInteger got = 0;
    NSMutableData *cbw_data = [NSMutableData dataWithBytes:&cbw length:31];
    if (![g_out sendIORequestWithData:cbw_data
                     bytesTransferred:&got
                    completionTimeout:8.0
                                error:&err] ||
        got != 31) {
        fprintf(stderr, "  CBW send failed (got %lu)\n", (unsigned long)got);
        return -1;
    }

    if (rtl_cancel_signal) return -6;
    NSUInteger data_got = 0;
    if (data_len) {
        NSMutableData *data = [NSMutableData dataWithLength:data_len];
        err = nil;
        if (![g_in sendIORequestWithData:data
                        bytesTransferred:&data_got
                       completionTimeout:8.0
                                   error:&err]) {
            fprintf(stderr, "  data-in transfer failed\n");
            if (rtl_cancel_signal) return -6;
            NSError *clear_error = nil;
            [g_in clearStallWithError:&clear_error];
            return -2;
        }
        if (data_got != data_len) {
            fprintf(stderr, "  short data-in: expected %u got %lu\n", data_len,
                    (unsigned long)data_got);
            return -2;
        }
        memcpy(buf, data.bytes, data_len);
    }

    if (rtl_cancel_signal) return -6;
    NSMutableData *csw_data = [NSMutableData dataWithLength:13];
    NSUInteger csw_got = 0;
    err = nil;
    if (![g_in sendIORequestWithData:csw_data
                    bytesTransferred:&csw_got
                   completionTimeout:8.0
                               error:&err]) {
        if (rtl_cancel_signal) return -6;
        NSError *clear_error = nil;
        [g_in clearStallWithError:&clear_error];
        err = nil;
        csw_got = 0;
        [g_in sendIORequestWithData:csw_data
                   bytesTransferred:&csw_got
                  completionTimeout:8.0
                              error:&err];
    }
    if (csw_got != 13) {
        fprintf(stderr, "  CSW read got %lu\n", (unsigned long)csw_got);
        return -3;
    }

    csw_t csw;
    memcpy(&csw, csw_data.bytes, 13);
    if (csw.sig != 0x53425355u || csw.tag != cbw.tag) {
        fprintf(stderr, "  bad CSW signature or tag\n");
        return -4;
    }
    fprintf(stderr, "  CSW: status=%u residue=%u data_got=%lu\n", csw.status,
            csw.residue, (unsigned long)data_got);
    if (csw.status != 0 || csw.residue != 0) {
        fprintf(stderr, "  command failed: status=%u residue=%u\n", csw.status,
                csw.residue);
        return -5;
    }
    return 0;
}

static void build_realtek_cdb(uint8_t *cdb, uint16_t size, uint8_t opcode,
                              uint8_t cdw10_lo)
{
    memset(cdb, 0, 16);
    cdb[0] = 0xe4;
    cdb[1] = (uint8_t)(size & 0xff);
    cdb[2] = (uint8_t)(size >> 8);
    cdb[3] = opcode;
    cdb[4] = cdw10_lo;
}

static uint64_t le64_lo(const uint8_t *p)
{
    uint64_t value = 0;
    for (int i = 7; i >= 0; i--)
        value = (value << 8) | p[i];
    return value;
}

static void print_u128_field(const char *name, const uint8_t *p)
{
    bool high_nonzero = false;
    for (int i = 8; i < 16; i++)
        if (p[i])
            high_nonzero = true;
    printf("  %-34s %" PRIu64 "%s\n", name, le64_lo(p),
           high_nonzero ? " (upper 64 bits nonzero!)" : "");
}

static void trimmed_ascii(char *dst, const uint8_t *src, size_t length)
{
    memcpy(dst, src, length);
    dst[length] = 0;
    for (ssize_t i = (ssize_t)length - 1;
         i >= 0 && (dst[i] == ' ' || dst[i] == 0); i--)
        dst[i] = 0;
}

static int save_blob(const char *dir, const char *name, const uint8_t *buf,
                     size_t length)
{
    char path[1024];
    if (snprintf(path, sizeof path, "%s/%s", dir, name) >= (int)sizeof path) {
        fprintf(stderr, "FATAL: output path is too long\n");
        return -1;
    }
    FILE *file = fopen(path, "wb");
    if (!file) {
        fprintf(stderr, "FATAL: could not open output file %s\n", name);
        return -1;
    }
    size_t written = fwrite(buf, 1, length, file);
    int close_rc = fclose(file);
    if (written != length || close_rc != 0) {
        fprintf(stderr, "FATAL: could not persist complete %s\n", name);
        return -1;
    }
    fprintf(stderr, "  saved %s (%zu bytes)\n", name, length);
    return 0;
}

static bool parse_u16(const char *text, uint16_t *value)
{
    errno = 0;
    char *end = NULL;
    unsigned long parsed = strtoul(text, &end, 0);
    if (errno || !end || *end || parsed > UINT16_MAX)
        return false;
    *value = (uint16_t)parsed;
    return true;
}

static bool copy_profile_string(NSDictionary *profile, NSString *key,
                                char *dst, size_t capacity)
{
    id value = profile[key];
    if (![value isKindOfClass:[NSString class]] || [(NSString *)value length] == 0)
        return false;
    return [(NSString *)value getCString:dst
                               maxLength:capacity
                                encoding:NSUTF8StringEncoding];
}

static bool load_identity_profile(const char *path, identity_t *identity)
{
    NSDictionary *profile = [NSDictionary dictionaryWithContentsOfFile:
        [NSString stringWithUTF8String:path]];
    if (![profile isKindOfClass:[NSDictionary class]])
        return false;

    char vid[16] = {0};
    char pid[16] = {0};
    char bcd[16] = {0};
    return copy_profile_string(profile, @"usb_vid", vid, sizeof vid) &&
           copy_profile_string(profile, @"usb_pid", pid, sizeof pid) &&
           copy_profile_string(profile, @"usb_bcd", bcd, sizeof bcd) &&
           parse_u16(vid, &identity->vid) &&
           parse_u16(pid, &identity->pid) &&
           parse_u16(bcd, &identity->bcd) &&
           copy_profile_string(profile, @"usb_serial", identity->usb_serial,
                               sizeof identity->usb_serial) &&
           copy_profile_string(profile, @"nvme_model", identity->nvme_model,
                               sizeof identity->nvme_model) &&
           copy_profile_string(profile, @"nvme_serial", identity->nvme_serial,
                               sizeof identity->nvme_serial);
}

static bool usb_string_copy(io_service_t service, CFStringRef key, char *dst,
                            size_t capacity)
{
    CFTypeRef value = IORegistryEntryCreateCFProperty(
        service, key, kCFAllocatorDefault, kNilOptions);
    if (!value || CFGetTypeID(value) != CFStringGetTypeID()) {
        if (value)
            CFRelease(value);
        return false;
    }
    bool copied = CFStringGetCString((CFStringRef)value, dst, capacity,
                                     kCFStringEncodingUTF8);
    CFRelease(value);
    return copied && dst[0];
}

static bool usb_number_u16(io_service_t service, CFStringRef key,
                           uint16_t *result)
{
    CFTypeRef value = IORegistryEntryCreateCFProperty(
        service, key, kCFAllocatorDefault, kNilOptions);
    if (!value || CFGetTypeID(value) != CFNumberGetTypeID()) {
        if (value)
            CFRelease(value);
        return false;
    }
    int number = 0;
    bool copied = CFNumberGetValue((CFNumberRef)value, kCFNumberIntType, &number);
    CFRelease(value);
    if (!copied || number < 0 || number > UINT16_MAX)
        return false;
    *result = (uint16_t)number;
    return true;
}

// Resolve a whole BSD disk to its actual USB ancestor, never by product name.
static uint64_t disk_usb_id(const char *disk)
{
    if (strncmp(disk, "/dev/disk", 9) != 0 || !disk[9]) return 0;
    for (const char *p = disk + 9; *p; ++p)
        if (*p < '0' || *p > '9') return 0;
    io_service_t node = IOServiceGetMatchingService(kIOMainPortDefault,
        IOBSDNameMatching(kIOMainPortDefault, 0, disk + 5));
    while (node) {
        if (IOObjectConformsTo(node, "IOUSBHostDevice")) {
            uint64_t value = 0;
            IORegistryEntryGetRegistryEntryID(node, &value);
            IOObjectRelease(node);
            return value;
        }
        io_registry_entry_t parent = IO_OBJECT_NULL;
        kern_return_t result = IORegistryEntryGetParentEntry(node, kIOServicePlane, &parent);
        IOObjectRelease(node);
        node = result == KERN_SUCCESS ? parent : IO_OBJECT_NULL;
    }
    return 0;
}

static io_service_t find_exact_rtl(const identity_t *identity, bool enrolling)
{
    CFMutableDictionaryRef match = [IOUSBHostDevice
        createMatchingDictionaryWithVendorID:@(identity->vid)
                                   productID:@(identity->pid)
                                   bcdDevice:enrolling ? nil : @(identity->bcd)
                                 deviceClass:nil
                              deviceSubclass:nil
                              deviceProtocol:nil
                                       speed:nil
                              productIDArray:nil];
    io_iterator_t iterator = IO_OBJECT_NULL;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, match, &iterator) !=
        KERN_SUCCESS)
        return IO_OBJECT_NULL;

    io_service_t selected = IO_OBJECT_NULL;
    io_service_t candidate;
    int count = 0;
    while ((candidate = IOIteratorNext(iterator))) {
        bool accepted = enrolling;
        if (!enrolling) {
            char serial[128] = {0};
            accepted = usb_string_copy(candidate, CFSTR("USB Serial Number"),
                                       serial, sizeof serial) &&
                       strcmp(serial, identity->usb_serial) == 0;
        }
        if (accepted) {
            count++;
            if (!selected)
                selected = candidate;
            else
                IOObjectRelease(candidate);
        } else {
            IOObjectRelease(candidate);
        }
    }
    IOObjectRelease(iterator);
    if (count != 1) {
        if (selected)
            IOObjectRelease(selected);
        fprintf(stderr,
                "FATAL: expected exactly one configured RTL9210 bridge; found %d\n",
                count);
        return IO_OBJECT_NULL;
    }
    return selected;
}

static bool write_enrollment(const char *dir, const identity_t *identity)
{
    NSString *directory = [NSString stringWithUTF8String:dir];
    NSString *path = [directory stringByAppendingPathComponent:@"enrollment.plist"];
    NSDictionary *profile = @{
        @"usb_vid" : [NSString stringWithFormat:@"0x%04x", identity->vid],
        @"usb_pid" : [NSString stringWithFormat:@"0x%04x", identity->pid],
        @"usb_bcd" : [NSString stringWithFormat:@"0x%04x", identity->bcd],
        @"usb_serial" : [NSString stringWithUTF8String:identity->usb_serial],
        @"nvme_model" : [NSString stringWithUTF8String:identity->nvme_model],
        @"nvme_serial" : [NSString stringWithUTF8String:identity->nvme_serial],
    };
    return [profile writeToFile:path atomically:YES];
}

static IOUSBHostInterface *open_interface(io_service_t device_service,
                                          dispatch_queue_t queue)
{
    for (int attempt = 1; attempt <= 10; attempt++) {
        if (rtl_cancel_signal) return nil;
        mach_timespec_t wait = {2, 0};
        IOServiceWaitQuiet(device_service, &wait);
        if (rtl_cancel_signal) return nil;
        io_iterator_t iterator = IO_OBJECT_NULL;
        if (IORegistryEntryGetChildIterator(device_service, kIOServicePlane,
                                            &iterator) != KERN_SUCCESS) {
            usleep(300 * 1000);
            continue;
        }

        io_object_t child;
        IOUSBHostInterface *interface = nil;
        while ((child = IOIteratorNext(iterator))) {
            if (!IOObjectConformsTo(child, "IOUSBHostInterface")) {
                IOObjectRelease(child);
                continue;
            }
            uint16_t interface_number = UINT16_MAX;
            if (!usb_number_u16(child, CFSTR("bInterfaceNumber"),
                                &interface_number) ||
                interface_number != 0) {
                IOObjectRelease(child);
                continue;
            }
            io_iterator_t grandchildren = IO_OBJECT_NULL;
            bool has_driver = false;
            if (IORegistryEntryGetChildIterator(child, kIOServicePlane,
                                                &grandchildren) == KERN_SUCCESS) {
                io_object_t grandchild;
                while ((grandchild = IOIteratorNext(grandchildren))) {
                    io_name_t class_name;
                    IOObjectGetClass(grandchild, class_name);
                    if (!strstr(class_name, "UserClient"))
                        has_driver = true;
                    IOObjectRelease(grandchild);
                }
                IOObjectRelease(grandchildren);
            }
            fprintf(stderr, "attempt %d: interface driver=%s\n", attempt,
                    has_driver ? "YES" : "no");
            NSError *interface_error = nil;
            interface = [[IOUSBHostInterface alloc]
                initWithIOService:child
                          options:IOUSBHostObjectInitOptionsNone
                            queue:queue
                            error:&interface_error
                  interestHandler:nil];
            if (!interface)
                fprintf(stderr, "  interface open failed\n");
            IOObjectRelease(child);
            if (interface)
                break;
        }
        IOObjectRelease(iterator);
        if (interface)
            return interface;
        usleep(400 * 1000);
    }
    return nil;
}

int main(int argc, char **argv)
{
    if (argc == 3 && strcmp(argv[1], "binding") == 0) {
        uint64_t value = disk_usb_id(argv[2]);
        if (!value) return 1;
        printf("%" PRIu64 "\n", value);
        return 0;
    }
    bool enrolling = argc == 7 && strcmp(argv[1], "enroll") == 0;
    bool probing = argc == 6 && strcmp(argv[1], "probe") == 0;
    if (!enrolling && !probing) {
        fprintf(stderr, "FATAL: run through the rtl9210-health front door\n");
        return 2;
    }
    if (geteuid() != 0) {
        fprintf(stderr, "FATAL: run through the rtl9210-health front door\n");
        return 2;
    }

    if (rtl_install_cancellation() != 0) return 2;
    const char *selected_disk = argv[argc - 2];
    const char *id_text = argv[argc - 1];
    char *end = NULL;
    errno = 0;
    uint64_t expected_id = strtoull(id_text, &end, 10);
    if (errno || !expected_id || !*id_text || *end || *id_text == '-') return 2;
    const char *outdir = argv[2];
    identity_t identity = {0};
    if (enrolling) {
        if (!parse_u16(argv[3], &identity.vid) ||
            !parse_u16(argv[4], &identity.pid)) {
            fprintf(stderr, "FATAL: invalid enrollment VID/PID\n");
            return 2;
        }
    }

    @autoreleasepool {
        if (probing && !load_identity_profile(argv[3], &identity)) {
            fprintf(stderr, "FATAL: invalid or incomplete device profile\n");
            return 2;
        }

        dispatch_queue_t queue =
            dispatch_queue_create("rtl9210.health", DISPATCH_QUEUE_SERIAL);
        io_service_t service = IO_OBJECT_NULL;
        IOUSBHostDevice *device = nil;
        NSError *error = nil;
        int rc = 1;
        bool captured = false;
        bool have_identify = false;
        bool have_smart = false;
        bool have_error_log = false;
        uint8_t cdb[16] = {0};
        uint8_t inquiry[36] = {0};
        static uint8_t identify[4096];
        static uint8_t smart[512];
        static uint8_t error_log[512];

        if (rtl_cancel_signal) goto done;
        service = find_exact_rtl(&identity, enrolling);
        if (!service) {
            fprintf(stderr, "FATAL: configured RTL9210 bridge unavailable\n");
            goto done;
        }

        if (enrolling &&
            (!usb_number_u16(service, CFSTR("bcdDevice"), &identity.bcd) ||
             !usb_string_copy(service, CFSTR("USB Serial Number"),
                              identity.usb_serial,
                              sizeof identity.usb_serial))) {
            fprintf(stderr, "FATAL: bridge identity is incomplete\n");
            goto done;
        }

        uint64_t selected_id = 0;
        if (IORegistryEntryGetRegistryEntryID(service, &selected_id) != KERN_SUCCESS ||
            selected_id != expected_id || disk_usb_id(selected_disk) != expected_id) {
            fprintf(stderr, "FATAL: selected disk/USB registry binding changed; refusing capture\n");
            goto done;
        }
        if (rtl_cancel_signal) goto done;
        device = [[IOUSBHostDevice alloc]
            initWithIOService:service
                      options:IOUSBHostObjectInitOptionsDeviceCapture
                        queue:queue
                        error:&error
              interestHandler:nil];
        if (!device) {
            fprintf(stderr, "FATAL: USB device capture failed\n");
            goto done;
        }
        captured = true;
        if (rtl_cancel_signal) goto done;
        fprintf(stderr, "capture OK\n");

        const IOUSBConfigurationDescriptor *layout =
            [device configurationDescriptorWithConfigurationValue:1 error:&error];
        if (!validate_bot_layout(layout)) {
            fprintf(stderr, "FATAL: unsupported or malformed BOT descriptors; no vendor commands sent\n");
            goto done;
        }
        fprintf(stderr, "validated configuration 1, interface 0/BOT, bulk IN 0x81 OUT 0x02\n");

        if (rtl_cancel_signal) goto done;
        BOOL unconfigured =
            [device configureWithValue:0 matchInterfaces:NO error:&error];
        fprintf(stderr, "configureWithValue(0) -> %s\n",
                unconfigured ? "OK" : "FAILED");
        if (!unconfigured)
            goto done;

        error = nil;
        mach_timespec_t wait = {2, 0};
        IOServiceWaitQuiet([device ioService], &wait);

        if (rtl_cancel_signal) goto done;
        BOOL configured =
            [device configureWithValue:1 matchInterfaces:NO error:&error];
        fprintf(stderr,
                "configureWithValue(1, matchInterfaces=NO) -> %s\n",
                configured ? "OK" : "FAILED");
        if (!configured)
            goto done;

        if (rtl_cancel_signal) goto done;
        g_intf = open_interface([device ioService], queue);
        if (!g_intf) {
            fprintf(stderr, "FATAL: could not open interface 0\n");
            goto done;
        }
        fprintf(stderr, "interface OPEN\n");

        if (rtl_cancel_signal) goto done;
        error = nil;
        if (![g_intf selectAlternateSetting:0 error:&error]) {
            fprintf(stderr, "FATAL: could not select BOT alternate setting\n");
            goto done;
        }

        if (rtl_cancel_signal) goto done;
        const IOUSBInterfaceDescriptor *active = g_intf.interfaceDescriptor;
        if (!active || active->bInterfaceNumber != 0 || active->bAlternateSetting != 0 ||
            active->bInterfaceClass != 8 || active->bInterfaceSubClass != 6 ||
            active->bInterfaceProtocol != 0x50 || active->bNumEndpoints != 2) {
            fprintf(stderr, "FATAL: active interface is not the validated BOT interface\n");
            goto done;
        }
        error = nil;
        g_in = [g_intf copyPipeWithAddress:0x81 error:&error];
        error = nil;
        g_out = [g_intf copyPipeWithAddress:0x02 error:&error];
        if (!g_in || !g_out) {
            fprintf(stderr, "FATAL: expected bulk pipes are unavailable\n");
            goto done;
        }
        fprintf(stderr, "pipes ready\n");

        fprintf(stderr, "\n== SCSI INQUIRY ==\n");
        memset(cdb, 0, sizeof cdb);
        cdb[0] = 0x12;
        cdb[4] = 36;
        if (bot_command_in(cdb, 6, inquiry, 36) != 0)
            goto done;
        char vendor[9], product[17], revision[5];
        trimmed_ascii(vendor, inquiry + 8, 8);
        trimmed_ascii(product, inquiry + 16, 16);
        trimmed_ascii(revision, inquiry + 32, 4);
        printf("INQUIRY: vendor='%s' product='%s' rev='%s'\n", vendor, product,
               revision);

        fprintf(stderr, "\n== NVMe Identify Controller ==\n");
        build_realtek_cdb(cdb, 4096, 0x06, 0x01);
        have_identify = bot_command_in(cdb, 16, identify, 4096) == 0;
        if (!have_identify)
            goto done;

        char serial[21], model[41], firmware[9];
        trimmed_ascii(serial, identify + 4, 20);
        trimmed_ascii(model, identify + 24, 40);
        trimmed_ascii(firmware, identify + 64, 8);
        printf("\nNVMe Identify Controller:\n");
        printf("  %-34s %s\n", "Model:", model);
        printf("  %-34s %s\n", "Serial:",
               enrolling ? "[captured privately during enrollment]"
                         : "[matched enrolled device]");
        printf("  %-34s %s\n", "Firmware:", firmware);

        if (enrolling) {
            if (!model[0] || !serial[0] || strlen(model) > 40 ||
                strlen(serial) > 20) {
                fprintf(stderr, "FATAL: invalid underlying NVMe identity\n");
                goto done;
            }
            memcpy(identity.nvme_model, model, sizeof identity.nvme_model);
            memcpy(identity.nvme_serial, serial, sizeof identity.nvme_serial);
        } else if (strcmp(model, identity.nvme_model) != 0 ||
                   strcmp(serial, identity.nvme_serial) != 0) {
            fprintf(stderr, "FATAL: underlying NVMe identity mismatch\n");
            goto done;
        }

        if (save_blob(outdir, "identify_controller.bin", identify,
                      sizeof identify) != 0)
            goto done;

        fprintf(stderr, "\n== NVMe SMART/Health Log 0x02 ==\n");
        build_realtek_cdb(cdb, 512, 0x02, 0x02);
        have_smart = bot_command_in(cdb, 16, smart, 512) == 0;
        if (!have_smart ||
            save_blob(outdir, "smart_health_log.bin", smart, sizeof smart) != 0)
            goto done;

        int temperature_kelvin = smart[1] | (smart[2] << 8);
        printf("\nNVMe SMART / Health Information:\n");
        printf("  %-34s 0x%02x%s\n", "Critical Warning:", smart[0],
               smart[0] ? "  <-- NONZERO" : " (none)");
        printf("  %-34s %d C\n", "Composite Temperature:",
               temperature_kelvin - 273);
        printf("  %-34s %u%%\n", "Available Spare:", smart[3]);
        printf("  %-34s %u%%\n", "Available Spare Threshold:", smart[4]);
        printf("  %-34s %u%%\n", "Percentage Used:", smart[5]);
        print_u128_field("Data Units Read (x512,000B):", smart + 32);
        print_u128_field("Data Units Written (x512,000B):", smart + 48);
        print_u128_field("Host Read Commands:", smart + 64);
        print_u128_field("Host Write Commands:", smart + 80);
        print_u128_field("Controller Busy Time (min):", smart + 96);
        print_u128_field("Power Cycles:", smart + 112);
        print_u128_field("Power On Hours:", smart + 128);
        print_u128_field("Unsafe Shutdowns:", smart + 144);
        print_u128_field("Media/Data Integrity Errors:", smart + 160);
        print_u128_field("Error Info Log Entries:", smart + 176);

        fprintf(stderr, "\n== NVMe Error Information Log 0x01 ==\n");
        build_realtek_cdb(cdb, 512, 0x02, 0x01);
        have_error_log = bot_command_in(cdb, 16, error_log, 512) == 0;
        if (!have_error_log ||
            save_blob(outdir, "error_info_log.bin", error_log,
                      sizeof error_log) != 0)
            goto done;

        bool nonzero_error_log = false;
        for (size_t i = 0; i < sizeof error_log; i++)
            if (error_log[i])
                nonzero_error_log = true;
        printf("\nError Information Log: %s\n",
               nonzero_error_log
                   ? "nonzero entries present (see private raw log)"
                   : "first 512 bytes are zero; not a complete lifetime error history");

        /* 512 bytes cover at most eight 64-byte error records, not all entries
         * supported by a controller. Status codes require NVMe interpretation;
         * a nonzero entry alone is not evidence of failed NAND. */
        printf("  Captured error records: first 8 only (512 bytes)\n");
        for (size_t n = 0; n < sizeof error_log / 64; n++) {
            const uint8_t *entry = error_log + n * 64;
            uint64_t count = le64_lo(entry);
            if (!count) continue;
            unsigned status = entry[12] | ((unsigned)entry[13] << 8);
            unsigned sqid = entry[8] | ((unsigned)entry[9] << 8);
            printf("  entry %zu: count=%" PRIu64 " sqid=%u status_raw=0x%04x lba=%" PRIu64 "\n",
                   n, count, sqid, status, le64_lo(entry + 16));
        }

        if (rtl_cancel_signal) goto done;
        if (enrolling && !write_enrollment(outdir, &identity)) {
            fprintf(stderr, "FATAL: could not save private enrollment profile\n");
            goto done;
        }
        rc = 0;

done:
        g_in = nil;
        g_out = nil;
        if (g_intf) {
            [g_intf destroy];
            g_intf = nil;
        }
        if (device) {
            [device destroy];
            device = nil;
            fprintf(stderr,
                    "\n[restore] capture released; reset/rematch requested, volume recovery NOT yet verified\n");
        }
        if (service)
            IOObjectRelease(service);

        // Written only after destroy calls returned; not proof of physical recovery.
        NSDictionary *release_status = @{
            @"release_path_completed": @YES,
            @"capture_acquired": @(captured),
            @"cancel_signal": @((int)rtl_cancel_signal)
        };
        NSString *release_path = [[NSString stringWithUTF8String:outdir]
            stringByAppendingPathComponent:@"release-status.plist"];
        if (![release_status writeToFile:release_path atomically:YES]) rc = 1;
        if (rtl_cancel_signal) rc = 128 + rtl_cancel_signal;
        printf("\nprobe result: identify=%s smart=%s error_log=%s\n",
               have_identify ? "OK" : "FAILED",
               have_smart ? "OK" : "FAILED",
               have_error_log ? "OK" : "FAILED");
        return rc;
    }
}
