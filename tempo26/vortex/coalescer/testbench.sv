`timescale 1ns/1ps
`default_nettype none

module VX_mem_coalescer_tb;

    // Main configuration: two wide output groups, non-zero flags and UUID.
    localparam integer N_REQS   = 8;
    localparam integer ADDRW    = 12;
    localparam integer FLAGSW   = 3;
    localparam integer IN_SIZE  = 4;
    localparam integer OUT_SIZE = 16;
    localparam integer TAGW     = 8;
    localparam integer UUIDW    = 2;
    localparam integer QSIZE    = 4;
    localparam integer PCW      = 3;
    localparam integer INW      = IN_SIZE * 8;
    localparam integer OUTW     = OUT_SIZE * 8;
    localparam integer RATIO    = OUT_SIZE / IN_SIZE;
    localparam integer RATIOW   = $clog2(RATIO);
    localparam integer OREQS    = N_REQS / RATIO;
    localparam integer OADDRW   = ADDRW - RATIOW;
    localparam integer QADDRW   = $clog2(QSIZE);
    localparam integer OTAGW    = UUIDW + QADDRW;
    localparam integer TAGIDW   = TAGW - UUIDW;

    reg clk;
    reg reset;
    always #5 clk = ~clk;

    wire [PCW-1:0] m_misses;
    reg m_in_req_valid;
    reg m_in_req_rw;
    reg [N_REQS-1:0] m_in_req_mask;
    reg [N_REQS*IN_SIZE-1:0] m_in_req_byteen;
    reg [N_REQS*ADDRW-1:0] m_in_req_addr;
    reg [N_REQS*FLAGSW-1:0] m_in_req_flags;
    reg [N_REQS*INW-1:0] m_in_req_data;
    reg [TAGW-1:0] m_in_req_tag;
    wire m_in_req_ready;

    wire m_in_rsp_valid;
    wire [N_REQS-1:0] m_in_rsp_mask;
    wire [N_REQS*INW-1:0] m_in_rsp_data;
    wire [TAGW-1:0] m_in_rsp_tag;
    reg m_in_rsp_ready;

    wire m_out_req_valid;
    wire m_out_req_rw;
    wire [OREQS-1:0] m_out_req_mask;
    wire [OREQS*OUT_SIZE-1:0] m_out_req_byteen;
    wire [OREQS*OADDRW-1:0] m_out_req_addr;
    wire [OREQS*FLAGSW-1:0] m_out_req_flags;
    wire [OREQS*OUTW-1:0] m_out_req_data;
    wire [OTAGW-1:0] m_out_req_tag;
    reg m_out_req_ready;

    reg m_out_rsp_valid;
    reg [OREQS-1:0] m_out_rsp_mask;
    reg [OREQS*OUTW-1:0] m_out_rsp_data;
    reg [OTAGW-1:0] m_out_rsp_tag;
    wire m_out_rsp_ready;

    VX_mem_coalescer #(
        .NUM_REQS      (N_REQS),
        .ADDR_WIDTH    (ADDRW),
        .FLAGS_WIDTH   (FLAGSW),
        .DATA_IN_SIZE  (IN_SIZE),
        .DATA_OUT_SIZE (OUT_SIZE),
        .TAG_WIDTH     (TAGW),
        .UUID_WIDTH    (UUIDW),
        .QUEUE_SIZE    (QSIZE),
        .PERF_CTR_BITS (PCW)
    ) dut_main (
        .clk            (clk),
        .reset          (reset),
        .misses         (m_misses),
        .in_req_valid   (m_in_req_valid),
        .in_req_rw      (m_in_req_rw),
        .in_req_mask    (m_in_req_mask),
        .in_req_byteen  (m_in_req_byteen),
        .in_req_addr    (m_in_req_addr),
        .in_req_flags   (m_in_req_flags),
        .in_req_data    (m_in_req_data),
        .in_req_tag     (m_in_req_tag),
        .in_req_ready   (m_in_req_ready),
        .in_rsp_valid   (m_in_rsp_valid),
        .in_rsp_mask    (m_in_rsp_mask),
        .in_rsp_data    (m_in_rsp_data),
        .in_rsp_tag     (m_in_rsp_tag),
        .in_rsp_ready   (m_in_rsp_ready),
        .out_req_valid  (m_out_req_valid),
        .out_req_rw     (m_out_req_rw),
        .out_req_mask   (m_out_req_mask),
        .out_req_byteen (m_out_req_byteen),
        .out_req_addr   (m_out_req_addr),
        .out_req_flags  (m_out_req_flags),
        .out_req_data   (m_out_req_data),
        .out_req_tag    (m_out_req_tag),
        .out_req_ready  (m_out_req_ready),
        .out_rsp_valid  (m_out_rsp_valid),
        .out_rsp_mask   (m_out_rsp_mask),
        .out_rsp_data   (m_out_rsp_data),
        .out_rsp_tag    (m_out_rsp_tag),
        .out_rsp_ready  (m_out_rsp_ready)
    );

    // Expected coalesced batches produced by the behavioral reference model.
    reg [OREQS-1:0] exp_mask [0:N_REQS-1];
    reg [OREQS*OADDRW-1:0] exp_addr [0:N_REQS-1];
    reg [OREQS*FLAGSW-1:0] exp_flags [0:N_REQS-1];
    reg [OREQS*OUT_SIZE-1:0] exp_byteen [0:N_REQS-1];
    reg [OREQS*OUTW-1:0] exp_data [0:N_REQS-1];
    reg [N_REQS-1:0] exp_pmask [0:N_REQS-1];
    integer exp_count;

    // Reference state for outstanding reads and the index allocator.
    reg [QSIZE-1:0] ref_free;
    reg ref_full;
    integer ref_alloc_addr;
    reg [TAGIDW-1:0] ref_tag_id [0:QSIZE-1];
    reg [UUIDW-1:0] ref_uuid [0:QSIZE-1];
    reg [N_REQS-1:0] ref_pmask [0:QSIZE-1];
    reg [N_REQS*RATIOW-1:0] ref_offsets [0:QSIZE-1];
    reg [OREQS-1:0] ref_rsp_remaining [0:QSIZE-1];
    reg [PCW-1:0] ref_misses;
    integer last_alloc_slot;
    integer last_packet_slots [0:N_REQS-1];
    integer last_packet_slot_count;

    integer errors;
    integer checks;
    reg cov_parameter_checks;
    reg cov_reset;
    reg cov_reset_pending;
    reg cov_single_batch;
    reg cov_multi_batch;
    reg cov_two_output_groups;
    reg cov_byte_conflict;
    reg cov_request_backpressure;
    reg cov_read_response;
    reg cov_response_backpressure;
    reg cov_partial_response;
    reg cov_out_of_order;
    reg cov_queue_full;
    reg cov_queue_recovery;
    reg cov_miss_increment;
    reg cov_miss_wrap;
    reg cov_zero_flags_uuid;

    task automatic fail(input string message);
        begin
            errors = errors + 1;
            $display("ERROR at %0t: %s", $time, message);
        end
    endtask

    task automatic clear_main_request;
        begin
            m_in_req_valid  = 1'b0;
            m_in_req_rw     = 1'b0;
            m_in_req_mask   = '0;
            m_in_req_byteen = '0;
            m_in_req_addr   = '0;
            m_in_req_flags  = '0;
            m_in_req_data   = '0;
            m_in_req_tag    = '0;
        end
    endtask

    task automatic reset_reference;
        integer i;
        begin
            ref_free       = {QSIZE{1'b1}};
            ref_full       = 1'b0;
            ref_alloc_addr = 0;
            ref_misses     = '0;
            last_packet_slot_count = 0;
            for (i = 0; i < QSIZE; i = i + 1) begin
                ref_tag_id[i]       = '0;
                ref_uuid[i]         = '0;
                ref_pmask[i]        = '0;
                ref_offsets[i]      = '0;
                ref_rsp_remaining[i]= '0;
            end
        end
    endtask

    function automatic integer lowest_free_slot;
        integer i;
        begin
            lowest_free_slot = -1;
            for (i = QSIZE - 1; i >= 0; i = i - 1) begin
                if (ref_free[i])
                    lowest_free_slot = i;
            end
        end
    endfunction

    task automatic reference_allocate;
        integer next_slot;
        begin
            if (ref_full)
                fail("reference allocator was asked to allocate while full");
            last_alloc_slot = ref_alloc_addr;
            ref_free[last_alloc_slot] = 1'b0;
            next_slot = lowest_free_slot();
            if (next_slot < 0) begin
                ref_full = 1'b1;
                ref_alloc_addr = 0;
            end else begin
                ref_full = 1'b0;
                ref_alloc_addr = next_slot;
            end
        end
    endtask

    task automatic reference_release(input integer slot);
        integer next_slot;
        reg was_full;
        begin
            if (ref_free[slot])
                fail("reference allocator detected a duplicate release");
            was_full = ref_full;
            ref_free[slot] = 1'b1;
            next_slot = lowest_free_slot();
            ref_full = 1'b0;
            // The Vortex allocator updates its selected address on a release
            // only when it was previously full.
            if (was_full)
                ref_alloc_addr = next_slot;
        end
    endtask

    // Build every expected batch for the currently prepared input packet.
    task automatic build_reference_batches;
        reg [N_REQS-1:0] remaining;
        reg [N_REQS-1:0] batch_lanes;
        reg [OADDRW-1:0] seed_base;
        integer b;
        integer g;
        integer lane;
        integer byte_idx;
        integer seed;
        integer offset;
        begin
            for (b = 0; b < N_REQS; b = b + 1) begin
                exp_mask[b]   = '0;
                exp_addr[b]   = '0;
                exp_flags[b]  = '0;
                exp_byteen[b] = '0;
                exp_data[b]   = '0;
                exp_pmask[b]  = '0;
            end

            remaining = {N_REQS{1'b1}};
            exp_count = 0;
            while (|(m_in_req_mask & remaining)) begin
                if (exp_count >= N_REQS) begin
                    fail("reference model exceeded maximum batch count");
                    remaining = '0;
                end else begin
                    batch_lanes = '0;
                    for (g = 0; g < OREQS; g = g + 1) begin
                        seed = -1;
                        for (lane = g * RATIO;
                             lane < (g + 1) * RATIO;
                             lane = lane + 1) begin
                            if (m_in_req_mask[lane] && remaining[lane]
                             && seed < 0)
                                seed = lane;
                        end

                        if (seed >= 0) begin
                            exp_mask[exp_count][g] = 1'b1;
                            seed_base = m_in_req_addr[
                                seed * ADDRW + RATIOW +: OADDRW
                            ];
                            exp_addr[exp_count][g * OADDRW +: OADDRW]
                                = seed_base;
                            exp_flags[exp_count][g * FLAGSW +: FLAGSW]
                                = m_in_req_flags[seed * FLAGSW +: FLAGSW];

                            for (lane = g * RATIO;
                                 lane < (g + 1) * RATIO;
                                 lane = lane + 1) begin
                                if (m_in_req_mask[lane] && remaining[lane]
                                 && m_in_req_addr[
                                        lane * ADDRW + RATIOW +: OADDRW
                                    ] == seed_base) begin
                                    batch_lanes[lane] = 1'b1;
                                    exp_pmask[exp_count][lane] = 1'b1;
                                    offset = m_in_req_addr[
                                        lane * ADDRW +: RATIOW
                                    ];
                                    for (byte_idx = 0;
                                         byte_idx < IN_SIZE;
                                         byte_idx = byte_idx + 1) begin
                                        if (m_in_req_byteen[
                                                lane * IN_SIZE + byte_idx
                                            ]) begin
                                            exp_byteen[exp_count][
                                                g * OUT_SIZE
                                                + offset * IN_SIZE
                                                + byte_idx
                                            ] = 1'b1;
                                            exp_data[exp_count][
                                                g * OUTW
                                                + offset * INW
                                                + byte_idx * 8 +: 8
                                            ] = m_in_req_data[
                                                lane * INW
                                                + byte_idx * 8 +: 8
                                            ];
                                        end
                                    end
                                end
                            end
                        end
                    end
                    remaining = remaining & ~batch_lanes;
                    exp_count = exp_count + 1;
                end
            end

            if (exp_count == 1)
                cov_single_batch = 1'b1;
            if (exp_count > 1)
                cov_multi_batch = 1'b1;
        end
    endtask

    task automatic check_output_batch(
        input integer batch_number,
        input reg first_observation
    );
        integer g;
        integer expected_slot;
        reg [OTAGW-1:0] expected_tag;
        begin
            checks = checks + 1;
            if (m_out_req_rw !== m_in_req_rw)
                fail("out_req_rw does not match input operation");
            if (m_out_req_mask !== exp_mask[batch_number])
                fail("coalesced output mask mismatch");

            if (&exp_mask[batch_number])
                cov_two_output_groups = 1'b1;

            for (g = 0; g < OREQS; g = g + 1) begin
                if (exp_mask[batch_number][g]) begin
                    if (m_out_req_addr[g * OADDRW +: OADDRW]
                     !== exp_addr[batch_number][g * OADDRW +: OADDRW])
                        fail("coalesced wide address mismatch");
                    if (m_out_req_flags[g * FLAGSW +: FLAGSW]
                     !== exp_flags[batch_number][g * FLAGSW +: FLAGSW])
                        fail("seed-lane flags mismatch");
                    if (m_out_req_byteen[g * OUT_SIZE +: OUT_SIZE]
                     !== exp_byteen[batch_number][g * OUT_SIZE +: OUT_SIZE])
                        fail("merged byte-enable mismatch");
                    if (m_out_req_data[g * OUTW +: OUTW]
                     !== exp_data[batch_number][g * OUTW +: OUTW])
                        fail("merged wide data mismatch");
                end
            end

            if (!m_in_req_rw && !first_observation)
                expected_slot = last_packet_slots[batch_number];
            else
                expected_slot = ref_alloc_addr;
            expected_tag = {
                m_in_req_tag[TAGW-1 -: UUIDW],
                QADDRW'(expected_slot)
            };
            if (m_out_req_tag !== expected_tag)
                fail("wide request tag mismatch");

            if (!m_in_req_rw && first_observation) begin
                reference_allocate();
                if (last_alloc_slot != expected_slot)
                    fail("allocator reference state mismatch");
                ref_tag_id[last_alloc_slot] =
                    m_in_req_tag[TAGIDW-1:0];
                ref_uuid[last_alloc_slot] =
                    m_in_req_tag[TAGW-1 -: UUIDW];
                ref_pmask[last_alloc_slot] = exp_pmask[batch_number];
                for (g = 0; g < N_REQS; g = g + 1) begin
                    ref_offsets[last_alloc_slot][g * RATIOW +: RATIOW]
                        = m_in_req_addr[g * ADDRW +: RATIOW];
                end
                ref_rsp_remaining[last_alloc_slot] = exp_mask[batch_number];
                last_packet_slots[last_packet_slot_count] = last_alloc_slot;
                last_packet_slot_count = last_packet_slot_count + 1;
            end
        end
    endtask

    // Monitor a packet whose input fields and in_req_valid are already driven.
    task automatic monitor_prebuilt_packet(input integer stall_cycles);
        integer batch_number;
        integer stall_left;
        reg observed;
        reg ready_seen;
        reg [PCW-1:0] old_misses;
        begin
            batch_number = 0;
            stall_left = stall_cycles;
            observed = 1'b0;
            ready_seen = 1'b0;
            last_packet_slot_count = 0;

            while (batch_number < exp_count) begin
                @(negedge clk);

                if (m_in_req_ready) begin
                    if (batch_number != (exp_count - 1))
                        fail("in_req_ready asserted before the final batch");
                    if (ready_seen)
                        fail("in_req_ready asserted for more than one SEND cycle");
                    ready_seen = 1'b1;
                    m_in_req_valid = 1'b0;
                end

                if (m_out_req_valid) begin
                    check_output_batch(batch_number, !observed);
                    if (!observed)
                        observed = 1'b1;

                    if (!m_out_req_ready) begin
                        cov_request_backpressure = 1'b1;
                        if (stall_left > 0)
                            stall_left = stall_left - 1;
                        if (stall_left == 0)
                            m_out_req_ready = 1'b1;
                    end

                    if (m_out_req_ready) begin
                        old_misses = ref_misses;
                        @(posedge clk);
                        #1;
                        // The DUT updates req_rem_mask when a batch is formed.
                        // Therefore the counter increments when a non-final
                        // batch handshakes; the accumulated value is still the
                        // number of additional batches required by the packet.
                        if (batch_number < (exp_count - 1)) begin
                            ref_misses = ref_misses + 1'b1;
                            cov_miss_increment = 1'b1;
                            if (old_misses == {PCW{1'b1}}
                             && ref_misses == '0)
                                cov_miss_wrap = 1'b1;
                        end
                        if (m_misses !== ref_misses)
                            fail("miss counter mismatch");
                        batch_number = batch_number + 1;
                        observed = 1'b0;
                    end
                end
            end

            if (!ready_seen)
                fail("input request completed without an in_req_ready handshake");
            m_out_req_ready = 1'b1;
        end
    endtask

    task automatic run_prepared_packet(input integer stall_cycles);
        begin
            if (m_in_req_mask == '0)
                fail("test attempted to send an empty request mask");
            build_reference_batches();
            @(negedge clk);
            m_out_req_ready = (stall_cycles == 0);
            m_in_req_valid = 1'b1;
            monitor_prebuilt_packet(stall_cycles);
        end
    endtask

    task automatic check_response_now(
        input integer slot,
        input [OREQS-1:0] response_mask,
        input [OREQS*OUTW-1:0] response_data
    );
        reg [N_REQS-1:0] expected_mask;
        reg [TAGW-1:0] expected_tag;
        reg [INW-1:0] expected_lane_data;
        integer lane;
        integer group_id;
        integer offset;
        begin
            expected_mask = '0;
            expected_tag = {ref_uuid[slot], ref_tag_id[slot]};
            for (lane = 0; lane < N_REQS; lane = lane + 1) begin
                group_id = lane / RATIO;
                offset = ref_offsets[slot][lane * RATIOW +: RATIOW];
                expected_mask[lane] =
                    response_mask[group_id] && ref_pmask[slot][lane];
                expected_lane_data = response_data[
                    group_id * OUTW + offset * INW +: INW
                ];
                if (expected_mask[lane]
                 && m_in_rsp_data[lane * INW +: INW]
                    !== expected_lane_data)
                    fail("unmerged narrow response data mismatch");
            end

            checks = checks + 1;
            if (!m_in_rsp_valid)
                fail("in_rsp_valid did not follow out_rsp_valid");
            if (m_in_rsp_mask !== expected_mask)
                fail("reconstructed response lane mask mismatch");
            if (m_in_rsp_tag !== expected_tag)
                fail("reconstructed input response tag mismatch");
            if (m_out_rsp_ready !== m_in_rsp_ready)
                fail("response ready path is not transparent");
        end
    endtask

    task automatic send_response(
        input integer slot,
        input [OREQS-1:0] response_mask,
        input [OREQS*OUTW-1:0] response_data,
        input integer stall_cycles
    );
        reg [OREQS-1:0] next_remaining;
        integer i;
        begin
            if (response_mask == '0)
                fail("test attempted to send an empty response mask");
            if ((response_mask & ref_rsp_remaining[slot]) != response_mask)
                fail("response contains a group not pending for this tag");

            @(negedge clk);
            m_out_rsp_tag = {ref_uuid[slot], QADDRW'(slot)};
            m_out_rsp_mask = response_mask;
            m_out_rsp_data = response_data;
            m_in_rsp_ready = (stall_cycles == 0);
            m_out_rsp_valid = 1'b1;
            #1;
            check_response_now(slot, response_mask, response_data);

            for (i = 0; i < stall_cycles; i = i + 1) begin
                if (m_out_rsp_ready !== 1'b0)
                    fail("out_rsp_ready ignored input-side backpressure");
                cov_response_backpressure = 1'b1;
                @(posedge clk);
                @(negedge clk);
                #1;
                check_response_now(slot, response_mask, response_data);
            end

            m_in_rsp_ready = 1'b1;
            #1;
            check_response_now(slot, response_mask, response_data);
            @(posedge clk);
            #1;

            next_remaining = ref_rsp_remaining[slot] & ~response_mask;
            ref_rsp_remaining[slot] = next_remaining;
            if (next_remaining == '0)
                reference_release(slot);
            else
                cov_partial_response = 1'b1;

            cov_read_response = 1'b1;
            @(negedge clk);
            m_out_rsp_valid = 1'b0;
            m_out_rsp_mask = '0;
            m_out_rsp_data = '0;
            m_out_rsp_tag = '0;
        end
    endtask

    task automatic apply_main_reset;
        begin
            @(negedge clk);
            reset = 1'b1;
            repeat (2) @(posedge clk);
            #1;
            if (m_out_req_valid !== 1'b0)
                fail("reset did not clear out_req_valid");
            if (m_out_req_mask !== '0 || m_out_req_byteen !== '0
             || m_out_req_addr !== '0 || m_out_req_flags !== '0
             || m_out_req_data !== '0 || m_out_req_tag !== '0)
                fail("reset did not clear registered output payload");
            if (m_misses !== '0)
                fail("reset did not clear misses");
            if (m_in_req_ready !== 1'b0)
                fail("in_req_ready must be low in reset/WAIT without input");
            cov_reset = 1'b1;
            reset_reference();
            @(negedge clk);
            reset = 1'b0;
        end
    endtask

    // Second configuration covers FLAGS_WIDTH=0 and UUID_WIDTH=0.
    localparam integer ZN       = 4;
    localparam integer ZADDRW   = 12;
    localparam integer ZINS     = 4;
    localparam integer ZOUTS    = 16;
    localparam integer ZINW     = 32;
    localparam integer ZOUTW    = 128;
    localparam integer ZRATIOW  = 2;
    localparam integer ZOADDRW  = 10;
    localparam integer ZTAGW    = 8;
    localparam integer ZQSIZE   = 2;
    localparam integer ZQADDRW  = 1;

    reg z_reset;
    wire [2:0] z_misses;
    reg z_in_req_valid, z_in_req_rw;
    reg [ZN-1:0] z_in_req_mask;
    reg [ZN*ZINS-1:0] z_in_req_byteen;
    reg [ZN*ZADDRW-1:0] z_in_req_addr;
    reg [ZN-1:0] z_in_req_flags;
    reg [ZN*ZINW-1:0] z_in_req_data;
    reg [ZTAGW-1:0] z_in_req_tag;
    wire z_in_req_ready;
    wire z_in_rsp_valid;
    wire [ZN-1:0] z_in_rsp_mask;
    wire [ZN*ZINW-1:0] z_in_rsp_data;
    wire [ZTAGW-1:0] z_in_rsp_tag;
    reg z_in_rsp_ready;
    wire z_out_req_valid, z_out_req_rw;
    wire [0:0] z_out_req_mask;
    wire [ZOUTS-1:0] z_out_req_byteen;
    wire [ZOADDRW-1:0] z_out_req_addr;
    wire [0:0] z_out_req_flags;
    wire [ZOUTW-1:0] z_out_req_data;
    wire [ZQADDRW-1:0] z_out_req_tag;
    reg z_out_req_ready;
    reg z_out_rsp_valid;
    reg [0:0] z_out_rsp_mask;
    reg [ZOUTW-1:0] z_out_rsp_data;
    reg [ZQADDRW-1:0] z_out_rsp_tag;
    wire z_out_rsp_ready;

    VX_mem_coalescer #(
        .NUM_REQS      (ZN),
        .ADDR_WIDTH    (ZADDRW),
        .FLAGS_WIDTH   (0),
        .DATA_IN_SIZE  (ZINS),
        .DATA_OUT_SIZE (ZOUTS),
        .TAG_WIDTH     (ZTAGW),
        .UUID_WIDTH    (0),
        .QUEUE_SIZE    (ZQSIZE)
    ) dut_zero_options (
        .clk            (clk),
        .reset          (z_reset),
        .misses         (z_misses),
        .in_req_valid   (z_in_req_valid),
        .in_req_rw      (z_in_req_rw),
        .in_req_mask    (z_in_req_mask),
        .in_req_byteen  (z_in_req_byteen),
        .in_req_addr    (z_in_req_addr),
        .in_req_flags   (z_in_req_flags),
        .in_req_data    (z_in_req_data),
        .in_req_tag     (z_in_req_tag),
        .in_req_ready   (z_in_req_ready),
        .in_rsp_valid   (z_in_rsp_valid),
        .in_rsp_mask    (z_in_rsp_mask),
        .in_rsp_data    (z_in_rsp_data),
        .in_rsp_tag     (z_in_rsp_tag),
        .in_rsp_ready   (z_in_rsp_ready),
        .out_req_valid  (z_out_req_valid),
        .out_req_rw     (z_out_req_rw),
        .out_req_mask   (z_out_req_mask),
        .out_req_byteen (z_out_req_byteen),
        .out_req_addr   (z_out_req_addr),
        .out_req_flags  (z_out_req_flags),
        .out_req_data   (z_out_req_data),
        .out_req_tag    (z_out_req_tag),
        .out_req_ready  (z_out_req_ready),
        .out_rsp_valid  (z_out_rsp_valid),
        .out_rsp_mask   (z_out_rsp_mask),
        .out_rsp_data   (z_out_rsp_data),
        .out_rsp_tag    (z_out_rsp_tag),
        .out_rsp_ready  (z_out_rsp_ready)
    );

    task automatic test_zero_flag_uuid_configuration;
        begin
            @(negedge clk);
            z_reset = 1'b1;
            repeat (2) @(posedge clk);
            @(negedge clk);
            z_reset = 1'b0;

            z_in_req_rw = 1'b0;
            z_in_req_mask = 4'b1001;
            z_in_req_byteen = {ZN*ZINS{1'b1}};
            z_in_req_addr = '0;
            z_in_req_addr[0*ZADDRW +: ZADDRW] = 12'h140;
            z_in_req_addr[3*ZADDRW +: ZADDRW] = 12'h143;
            z_in_req_flags = {ZN{1'b1}};
            z_in_req_data = '0;
            z_in_req_tag = 8'ha5;
            z_out_req_ready = 1'b1;
            z_in_req_valid = 1'b1;

            while (!z_in_req_ready)
                @(negedge clk);
            z_in_req_valid = 1'b0;
            while (!z_out_req_valid)
                @(negedge clk);
            if (z_out_req_rw !== 1'b0 || z_out_req_mask !== 1'b1)
                fail("zero-option read request control mismatch");
            if (z_out_req_flags !== 1'b0)
                fail("FLAGS_WIDTH=0 output was not tied to zero");
            if (z_out_req_tag !== 1'b0)
                fail("UUID_WIDTH=0 first queue tag was not slot zero");
            if (z_out_req_addr !== 10'h050)
                fail("zero-option configuration address mismatch");
            if (z_out_req_byteen !== 16'hf00f)
                fail("zero-option configuration byte-enable mismatch");
            if (z_out_req_data !== '0)
                fail("zero-option configuration read payload mismatch");
            if (z_misses !== '0)
                fail("zero-option single-batch request changed misses");
            @(posedge clk);

            @(negedge clk);
            z_out_rsp_tag = 1'b0;
            z_out_rsp_mask = 1'b1;
            z_out_rsp_data = '0;
            z_out_rsp_data[0*ZINW +: ZINW] = 32'h01020304;
            z_out_rsp_data[3*ZINW +: ZINW] = 32'ha0b0c0d0;
            z_out_rsp_valid = 1'b1;
            #1;
            if (!z_in_rsp_valid || z_in_rsp_mask !== 4'b1001)
                fail("UUID=0 reconstructed response mask mismatch");
            if (z_in_rsp_data[0*ZINW +: ZINW] !== 32'h01020304
             || z_in_rsp_data[3*ZINW +: ZINW] !== 32'ha0b0c0d0)
                fail("UUID=0 reconstructed response data mismatch");
            if (z_in_rsp_tag !== 8'ha5)
                fail("UUID_WIDTH=0 did not restore the complete input tag");
            if (z_out_rsp_ready !== z_in_rsp_ready)
                fail("zero-option response ready path mismatch");
            @(posedge clk);
            @(negedge clk);
            z_out_rsp_valid = 1'b0;
            cov_zero_flags_uuid = 1'b1;
        end
    endtask

    integer q;
    integer slot_a;
    integer slot_b;
    integer partial_slot;
    integer full_slots [0:QSIZE-1];
    integer replacement_slot;
    integer wrap_iter;
    reg [OREQS*OUTW-1:0] response_payload;

    initial begin
        clk = 1'b0;
        reset = 1'b1;
        z_reset = 1'b1;
        errors = 0;
        checks = 0;
        cov_parameter_checks = 0;
        cov_reset = 0;
        cov_reset_pending = 0;
        cov_single_batch = 0;
        cov_multi_batch = 0;
        cov_two_output_groups = 0;
        cov_byte_conflict = 0;
        cov_request_backpressure = 0;
        cov_read_response = 0;
        cov_response_backpressure = 0;
        cov_partial_response = 0;
        cov_out_of_order = 0;
        cov_queue_full = 0;
        cov_queue_recovery = 0;
        cov_miss_increment = 0;
        cov_miss_wrap = 0;
        cov_zero_flags_uuid = 0;

        clear_main_request();
        m_in_rsp_ready = 1'b1;
        m_out_req_ready = 1'b1;
        m_out_rsp_valid = 1'b0;
        m_out_rsp_mask = '0;
        m_out_rsp_data = '0;
        m_out_rsp_tag = '0;

        z_in_req_valid = 0;
        z_in_req_rw = 0;
        z_in_req_mask = '0;
        z_in_req_byteen = '0;
        z_in_req_addr = '0;
        z_in_req_flags = '0;
        z_in_req_data = '0;
        z_in_req_tag = '0;
        z_in_rsp_ready = 1;
        z_out_req_ready = 1;
        z_out_rsp_valid = 0;
        z_out_rsp_mask = '0;
        z_out_rsp_data = '0;
        z_out_rsp_tag = '0;

        if (RATIO != 4 || RATIOW != 2 || OREQS != 2
         || OADDRW != 10 || QADDRW != 2 || OTAGW != 4
         || ZRATIOW != 2)
            fail("derived parameter calculation mismatch");
        else
            cov_parameter_checks = 1'b1;

        repeat (2) @(posedge clk);
        apply_main_reset();

        // Reset while an output request is pending under backpressure.
        clear_main_request();
        m_in_req_rw = 1'b1;
        m_in_req_mask = 8'b00000001;
        m_in_req_byteen[0*IN_SIZE +: IN_SIZE] = 4'hf;
        m_in_req_addr[0*ADDRW +: ADDRW] = 12'h040;
        m_in_req_data[0*INW +: INW] = 32'h12345678;
        m_out_req_ready = 1'b0;
        @(negedge clk);
        m_in_req_valid = 1'b1;
        while (!m_in_req_ready)
            @(negedge clk);
        m_in_req_valid = 1'b0;
        while (!m_out_req_valid)
            @(negedge clk);
        @(negedge clk);
        reset = 1'b1;
        @(posedge clk);
        #1;
        if (m_out_req_valid !== 1'b0 || m_misses !== '0)
            fail("reset failed to cancel a pending output request");
        else
            cov_reset_pending = 1'b1;
        reset_reference();
        @(negedge clk);
        reset = 1'b0;
        m_out_req_ready = 1'b1;

        // Single-batch write, two output groups, seed flags, byte merge,
        // same-byte conflict, and request backpressure stability.
        clear_main_request();
        m_in_req_rw = 1'b1;
        m_in_req_tag = 8'b10_001101;
        m_in_req_mask = 8'b10010011;
        m_in_req_addr[0*ADDRW +: ADDRW] = 12'h100;
        m_in_req_addr[1*ADDRW +: ADDRW] = 12'h100;
        m_in_req_addr[4*ADDRW +: ADDRW] = 12'h204;
        m_in_req_addr[7*ADDRW +: ADDRW] = 12'h207;
        m_in_req_flags[0*FLAGSW +: FLAGSW] = 3'b101;
        m_in_req_flags[1*FLAGSW +: FLAGSW] = 3'b010;
        m_in_req_flags[4*FLAGSW +: FLAGSW] = 3'b011;
        m_in_req_flags[7*FLAGSW +: FLAGSW] = 3'b111;
        m_in_req_byteen[0*IN_SIZE +: IN_SIZE] = 4'b0001;
        m_in_req_byteen[1*IN_SIZE +: IN_SIZE] = 4'b0001;
        m_in_req_byteen[4*IN_SIZE +: IN_SIZE] = 4'b1111;
        m_in_req_byteen[7*IN_SIZE +: IN_SIZE] = 4'b1111;
        m_in_req_data[0*INW +: INW] = 32'h00000011;
        m_in_req_data[1*INW +: INW] = 32'h00000022;
        m_in_req_data[4*INW +: INW] = 32'h44332211;
        m_in_req_data[7*INW +: INW] = 32'h88776655;
        cov_byte_conflict = 1'b1;
        run_prepared_packet(3);

        // Multi-batch write: group zero needs two addresses while group one
        // is inactive in the second batch. The second handshake increments misses.
        clear_main_request();
        m_in_req_rw = 1'b1;
        m_in_req_tag = 8'b01_000111;
        m_in_req_mask = 8'b00110011;
        m_in_req_addr[0*ADDRW +: ADDRW] = 12'h300;
        m_in_req_addr[1*ADDRW +: ADDRW] = 12'h304;
        m_in_req_addr[4*ADDRW +: ADDRW] = 12'h408;
        m_in_req_addr[5*ADDRW +: ADDRW] = 12'h409;
        m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
        m_in_req_data[0*INW +: INW] = 32'h10000000;
        m_in_req_data[1*INW +: INW] = 32'h20000000;
        m_in_req_data[4*INW +: INW] = 32'h40000000;
        m_in_req_data[5*INW +: INW] = 32'h50000000;
        run_prepared_packet(0);

        // One read batch with both output groups. Return it as two partial
        // response beats and apply response backpressure to the first beat.
        clear_main_request();
        m_in_req_rw = 1'b0;
        m_in_req_tag = 8'b11_010101;
        m_in_req_mask = 8'b10010101;
        m_in_req_addr[0*ADDRW +: ADDRW] = 12'h500;
        m_in_req_addr[2*ADDRW +: ADDRW] = 12'h502;
        m_in_req_addr[4*ADDRW +: ADDRW] = 12'h608;
        m_in_req_addr[7*ADDRW +: ADDRW] = 12'h60b;
        m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
        run_prepared_packet(0);
        partial_slot = last_packet_slots[0];
        response_payload = '0;
        response_payload[0*OUTW + 0*INW +: INW] = 32'h0000a000;
        response_payload[0*OUTW + 2*INW +: INW] = 32'h0000a002;
        response_payload[1*OUTW + 0*INW +: INW] = 32'h0000b000;
        response_payload[1*OUTW + 3*INW +: INW] = 32'h0000b003;
        send_response(partial_slot, 2'b01, response_payload, 2);
        if (ref_free[partial_slot])
            fail("partial response released metadata too early");
        send_response(partial_slot, 2'b10, response_payload, 0);

        // Two independent reads are returned in reverse order.
        clear_main_request();
        m_in_req_rw = 1'b0;
        m_in_req_tag = 8'b01_000011;
        m_in_req_mask = 8'b00000101;
        m_in_req_addr[0*ADDRW +: ADDRW] = 12'h700;
        m_in_req_addr[2*ADDRW +: ADDRW] = 12'h702;
        m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
        run_prepared_packet(0);
        slot_a = last_packet_slots[0];

        clear_main_request();
        m_in_req_rw = 1'b0;
        m_in_req_tag = 8'b10_111000;
        m_in_req_mask = 8'b10100000;
        m_in_req_addr[5*ADDRW +: ADDRW] = 12'h805;
        m_in_req_addr[7*ADDRW +: ADDRW] = 12'h807;
        m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
        run_prepared_packet(0);
        slot_b = last_packet_slots[0];

        response_payload = '0;
        response_payload[1*OUTW + 1*INW +: INW] = 32'hbbbb0001;
        response_payload[1*OUTW + 3*INW +: INW] = 32'hbbbb0003;
        send_response(slot_b, 2'b10, response_payload, 0);
        response_payload = '0;
        response_payload[0*OUTW + 0*INW +: INW] = 32'haaaa0000;
        response_payload[0*OUTW + 2*INW +: INW] = 32'haaaa0002;
        send_response(slot_a, 2'b01, response_payload, 0);
        cov_out_of_order = 1'b1;

        // Fill all four metadata slots with outstanding single-batch reads.
        for (q = 0; q < QSIZE; q = q + 1) begin
            clear_main_request();
            m_in_req_rw = 1'b0;
            m_in_req_tag = {2'b10, TAGIDW'(q + 1)};
            m_in_req_mask = 8'b00000001;
            m_in_req_addr[0*ADDRW +: ADDRW] = 12'h900 + q * 4;
            m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
            run_prepared_packet(0);
            full_slots[q] = last_packet_slots[0];
        end
        if (!ref_full)
            fail("reference queue did not become full");

        // A fifth read must remain blocked until one response releases a slot.
        clear_main_request();
        m_in_req_rw = 1'b0;
        m_in_req_tag = 8'b01_101010;
        m_in_req_mask = 8'b00000010;
        m_in_req_addr[1*ADDRW +: ADDRW] = 12'ha01;
        m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
        build_reference_batches();
        @(negedge clk);
        m_in_req_valid = 1'b1;
        repeat (4) begin
            @(negedge clk);
            if (m_in_req_ready || m_out_req_valid)
                fail("queue-full condition did not block the fifth read");
        end
        cov_queue_full = 1'b1;

        response_payload = '0;
        response_payload[0*OUTW +: INW] = 32'hface0001;
        send_response(full_slots[1], 2'b01, response_payload, 0);
        monitor_prebuilt_packet(0);
        replacement_slot = last_packet_slots[0];
        if (replacement_slot != full_slots[1])
            fail("queue did not reuse the slot released from the full state");
        else
            cov_queue_recovery = 1'b1;

        // Drain all entries that remain outstanding after slot reuse.
        response_payload = '0;
        send_response(full_slots[0], 2'b01, response_payload, 0);
        send_response(full_slots[2], 2'b01, response_payload, 0);
        send_response(full_slots[3], 2'b01, response_payload, 0);
        send_response(replacement_slot, 2'b01, response_payload, 0);

        // Generate enough two-batch writes to prove modulo counter wraparound.
        wrap_iter = 0;
        while (!cov_miss_wrap && wrap_iter < 10) begin
            clear_main_request();
            m_in_req_rw = 1'b1;
            m_in_req_tag = TAGW'(wrap_iter);
            m_in_req_mask = 8'b00000011;
            m_in_req_addr[0*ADDRW +: ADDRW] = 12'hb00;
            m_in_req_addr[1*ADDRW +: ADDRW] = 12'hb04;
            m_in_req_byteen = {N_REQS*IN_SIZE{1'b1}};
            m_in_req_data[0*INW +: INW] = 32'h11110000 + wrap_iter;
            m_in_req_data[1*INW +: INW] = 32'h22220000 + wrap_iter;
            run_prepared_packet(0);
            wrap_iter = wrap_iter + 1;
        end
        if (!cov_miss_wrap)
            fail("miss counter did not wrap at its configured width");

        test_zero_flag_uuid_configuration();

        if (!(cov_parameter_checks && cov_reset && cov_reset_pending
           && cov_single_batch && cov_multi_batch && cov_two_output_groups
           && cov_byte_conflict && cov_request_backpressure
           && cov_read_response && cov_response_backpressure
           && cov_partial_response && cov_out_of_order
           && cov_queue_full && cov_queue_recovery
           && cov_miss_increment && cov_miss_wrap
           && cov_zero_flags_uuid)) begin
            fail("one or more required coverage scenarios were not reached");
        end

        if (errors == 0) begin
            $display("===========Your Design Passed===========");
            $display("Checks performed: %0d", checks);
        end else begin
            $display("===========Error: %0d failures===========", errors);
            $fatal(1, "VX_mem_coalescer reference-model test failed");
        end
        $finish;
    end

    initial begin
        #200000;
        $fatal(1, "VX_mem_coalescer testbench timeout");
    end

endmodule

`default_nettype wire
