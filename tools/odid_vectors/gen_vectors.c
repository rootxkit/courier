/*
 * Test vectors for gateway/odid.py, produced by the Open Drone ID reference
 * library (opendroneid-core-c, Apache-2.0). P1-15.
 *
 * The decoder in gateway/odid.py is not written from memory of the standard
 * (CLAUDE.md: never write a wire-format offset from memory). This program
 * encodes pseudo-random messages with the reference encoder, decodes them
 * with the reference decoder, and prints both the bytes and the decoded
 * values as JSON. gateway/tests/test_odid.py decodes the same bytes and must
 * agree with every value.
 *
 *   git clone https://github.com/opendroneid/opendroneid-core-c
 *   gcc -O1 -I opendroneid-core-c/libopendroneid \
 *       tools/odid_vectors/gen_vectors.c \
 *       opendroneid-core-c/libopendroneid/opendroneid.c -lm -o gen_vectors
 *   ./gen_vectors > gateway/tests/data/odid_vectors.json
 *
 * The seed is fixed, so the output is reproducible for a given library commit.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "opendroneid.h"

static unsigned int state = 12345u;
static unsigned int next(void) { state = state * 1103515245u + 12345u; return (state >> 8) & 0xFFFFFF; }
static double uniform(double lo, double hi) { return lo + (hi - lo) * (next() / (double) 0xFFFFFF); }
static int pick(int lo, int hi) { return lo + (int) (next() % (unsigned) (hi - lo + 1)); }

static void hex(const uint8_t *bytes, int n) {
    printf("\"");
    for (int i = 0; i < n; i++) printf("%02x", bytes[i]);
    printf("\"");
}

static void id_string(char *out, int n) {
    const char *alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";
    int len = pick(1, n);
    memset(out, 0, (size_t) n + 1);
    for (int i = 0; i < len; i++) out[i] = alphabet[pick(0, 33)];
}

static void basic_id(int first) {
    ODID_BasicID_data in, out;
    ODID_BasicID_encoded enc;
    odid_initBasicIDData(&in);
    in.IDType = (ODID_idtype_t) pick(1, 2);
    in.UAType = (ODID_uatype_t) pick(0, 15);
    id_string(in.UASID, ODID_ID_SIZE);
    encodeBasicIDMessage(&enc, &in);
    decodeBasicIDMessage(&out, &enc);
    printf("%s{\"type\":\"basic_id\",\"hex\":", first ? "" : ",\n");
    hex((uint8_t *) &enc, ODID_MESSAGE_SIZE);
    printf(",\"id_type\":%d,\"ua_type\":%d,\"ua_id\":\"%s\"}", out.IDType, out.UAType, out.UASID);
}

static void location(int first) {
    ODID_Location_data in, out;
    ODID_Location_encoded enc;
    odid_initLocationData(&in);
    in.Status = (ODID_status_t) pick(0, 4);
    int invalid = pick(0, 5) == 0;
    in.Direction = invalid ? INV_DIR : (float) uniform(0, 359.9);
    in.SpeedHorizontal = invalid ? INV_SPEED_H : (float) uniform(0, 254);
    in.SpeedVertical = invalid ? INV_SPEED_V : (float) uniform(-62, 62);
    in.Latitude = uniform(-89, 89);
    in.Longitude = uniform(-179, 179);
    in.AltitudeBaro = invalid ? -1000 : (float) uniform(-900, 9000);
    in.AltitudeGeo = invalid ? -1000 : (float) uniform(-900, 9000);
    in.HeightType = (ODID_Height_reference_t) pick(0, 1);
    in.Height = invalid ? -1000 : (float) uniform(-900, 9000);
    in.HorizAccuracy = (ODID_Horizontal_accuracy_t) pick(0, 12);
    in.VertAccuracy = (ODID_Vertical_accuracy_t) pick(0, 6);
    in.BaroAccuracy = (ODID_Vertical_accuracy_t) pick(0, 6);
    in.SpeedAccuracy = (ODID_Speed_accuracy_t) pick(0, 4);
    in.TSAccuracy = (ODID_Timestamp_accuracy_t) pick(0, 15);
    in.TimeStamp = invalid ? 0xFFFF : (float) uniform(0, 3599.9);
    encodeLocationMessage(&enc, &in);
    decodeLocationMessage(&out, &enc);
    printf("%s{\"type\":\"location\",\"hex\":", first ? "" : ",\n");
    hex((uint8_t *) &enc, ODID_MESSAGE_SIZE);
    printf(",\"status\":%d,\"direction\":%.9g,\"speed_horizontal\":%.9g,\"speed_vertical\":%.9g,"
           "\"latitude\":%.10f,\"longitude\":%.10f,\"altitude_baro\":%.9g,\"altitude_geo\":%.9g,"
           "\"height_type\":%d,\"height\":%.9g,\"horiz_accuracy\":%d,\"vert_accuracy\":%d,"
           "\"baro_accuracy\":%d,\"speed_accuracy\":%d,\"ts_accuracy\":%d,\"timestamp\":%.9g}",
           out.Status, out.Direction, out.SpeedHorizontal, out.SpeedVertical, out.Latitude,
           out.Longitude, out.AltitudeBaro, out.AltitudeGeo, out.HeightType, out.Height,
           out.HorizAccuracy, out.VertAccuracy, out.BaroAccuracy, out.SpeedAccuracy,
           out.TSAccuracy, out.TimeStamp);
}

static void operator_id(int first) {
    ODID_OperatorID_data in, out;
    ODID_OperatorID_encoded enc;
    odid_initOperatorIDData(&in);
    in.OperatorIdType = ODID_OPERATOR_ID;
    id_string(in.OperatorId, ODID_ID_SIZE);
    encodeOperatorIDMessage(&enc, &in);
    decodeOperatorIDMessage(&out, &enc);
    printf("%s{\"type\":\"operator_id\",\"hex\":", first ? "" : ",\n");
    hex((uint8_t *) &enc, ODID_MESSAGE_SIZE);
    printf(",\"operator_id_type\":%d,\"operator_id\":\"%s\"}", out.OperatorIdType, out.OperatorId);
}

static void system_msg(int first) {
    ODID_System_data in, out;
    ODID_System_encoded enc;
    odid_initSystemData(&in);
    in.OperatorLocationType = (ODID_operator_location_type_t) pick(0, 2);
    in.ClassificationType = (ODID_classification_type_t) pick(0, 1);
    in.OperatorLatitude = uniform(-89, 89);
    in.OperatorLongitude = uniform(-179, 179);
    in.AreaCount = (uint16_t) pick(1, 100);
    in.AreaRadius = (uint16_t) (pick(0, 255) * 10);
    in.AreaCeiling = (float) uniform(-900, 9000);
    in.AreaFloor = (float) uniform(-900, 9000);
    in.CategoryEU = (ODID_category_EU_t) pick(0, 3);
    in.ClassEU = (ODID_class_EU_t) pick(0, 7);
    in.OperatorAltitudeGeo = (float) uniform(-900, 9000);
    in.Timestamp = (uint32_t) pick(0, 0x7FFFFF) * 100u;
    encodeSystemMessage(&enc, &in);
    decodeSystemMessage(&out, &enc);
    printf("%s{\"type\":\"system\",\"hex\":", first ? "" : ",\n");
    hex((uint8_t *) &enc, ODID_MESSAGE_SIZE);
    printf(",\"operator_location_type\":%d,\"classification_type\":%d,"
           "\"operator_latitude\":%.10f,\"operator_longitude\":%.10f,\"area_count\":%d,"
           "\"area_radius\":%d,\"area_ceiling\":%.9g,\"area_floor\":%.9g,\"category_eu\":%d,"
           "\"class_eu\":%d,\"operator_altitude_geo\":%.9g,\"timestamp\":%u}",
           out.OperatorLocationType, out.ClassificationType, out.OperatorLatitude,
           out.OperatorLongitude, out.AreaCount, out.AreaRadius, out.AreaCeiling, out.AreaFloor,
           out.CategoryEU, out.ClassEU, out.OperatorAltitudeGeo, out.Timestamp);
}

/* A pack of one Basic ID, one Location and one System message, and what the
 * reference decoder takes out of it. */
static void pack(int first) {
    ODID_BasicID_data b; ODID_Location_data l; ODID_System_data s;
    ODID_MessagePack_data data; ODID_MessagePack_encoded enc; ODID_UAS_Data uas;
    odid_initBasicIDData(&b); odid_initLocationData(&l); odid_initSystemData(&s);
    b.IDType = ODID_IDTYPE_SERIAL_NUMBER; b.UAType = ODID_UATYPE_HELICOPTER_OR_MULTIROTOR;
    id_string(b.UASID, ODID_ID_SIZE);
    l.Status = ODID_STATUS_AIRBORNE; l.Direction = (float) uniform(0, 359.9);
    l.SpeedHorizontal = (float) uniform(0, 30); l.SpeedVertical = (float) uniform(-5, 5);
    l.Latitude = uniform(41, 42); l.Longitude = uniform(44, 45);
    l.AltitudeGeo = (float) uniform(400, 900); l.Height = (float) uniform(0, 120);
    s.OperatorLatitude = uniform(41, 42); s.OperatorLongitude = uniform(44, 45);
    odid_initMessagePackData(&data);
    encodeBasicIDMessage(&data.Messages[0].basicId, &b);
    encodeLocationMessage(&data.Messages[1].location, &l);
    encodeSystemMessage(&data.Messages[2].system, &s);
    data.MsgPackSize = 3;
    encodeMessagePack(&enc, &data);
    odid_initUasData(&uas);
    int ok = decodeMessagePack(&uas, &enc);
    int size = 3 + ODID_MESSAGE_SIZE * data.MsgPackSize;
    printf("%s{\"type\":\"pack\",\"hex\":", first ? "" : ",\n");
    hex((uint8_t *) &enc, size);
    printf(",\"ok\":%d,\"ua_id\":\"%s\",\"latitude\":%.10f,\"longitude\":%.10f,"
           "\"altitude_geo\":%.9g,\"operator_latitude\":%.10f}",
           ok == ODID_SUCCESS, uas.BasicID[0].UASID, uas.Location.Latitude,
           uas.Location.Longitude, uas.Location.AltitudeGeo, uas.System.OperatorLatitude);
}

int main(void) {
    printf("[\n");
    int first = 1;
    for (int i = 0; i < 40; i++) {
        basic_id(first); first = 0;
        location(0);
        operator_id(0);
        system_msg(0);
    }
    for (int i = 0; i < 10; i++) pack(0);
    printf("\n]\n");
    return 0;
}
