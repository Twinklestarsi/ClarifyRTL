`timescale 1ns / 1ps
`default_nettype none

// Standalone miss-status holding register for a non-blocking cache.
// Requests are allocated into a free slot, equal cache-line addresses are
// linked in arrival order, and a fill replays every request in that chain.
module VX_cache_mshr #(
    parameter integer ADDRW = 16,
    parameter integer DATAW = 32,
    parameter integer MSHR_SIZE = 8,
    parameter integer COUNTW = 16,
    parameter integer TIMING_BUG = 0,
    parameter integer IDW = (MSHR_SIZE > 1) ? $clog2(MSHR_SIZE) : 1
) (
    input  wire                     clk,
    input  wire                     reset,
    input  wire                     flush,
    input  wire                     allocate_valid,
    input  wire [ADDRW-1:0]         allocate_addr,
    input  wire                     allocate_write,
    input  wire [DATAW-1:0]         allocate_data,
    input  wire [3:0]               allocate_mask,
    input  wire [7:0]               allocate_tag,
    output wire                     allocate_ready,
    output wire [IDW-1:0]           allocate_id,
    output wire                     allocate_pending,
    output wire [IDW-1:0]           allocate_previd,
    input  wire                     finalize_valid,
    input  wire                     finalize_release,
    input  wire                     finalize_pending,
    input  wire [IDW-1:0]           finalize_previd,
    input  wire [IDW-1:0]           finalize_id,
    input  wire                     fill_valid,
    input  wire [IDW-1:0]           fill_id,
    output wire [ADDRW-1:0]         fill_addr,
    output wire                     dequeue_valid,
    input  wire                     dequeue_ready,
    output wire [ADDRW-1:0]         dequeue_addr,
    output wire                     dequeue_write,
    output wire [DATAW-1:0]         dequeue_data,
    output wire [3:0]               dequeue_mask,
    output wire [7:0]               dequeue_tag,
    output wire [IDW-1:0]           dequeue_id,
    output wire [MSHR_SIZE-1:0]      valid_entries,
    output wire [IDW:0]             occupancy,
    output wire                     full,
    output logic [COUNTW-1:0]       allocation_count,
    output logic [COUNTW-1:0]       replay_count,
    output logic [COUNTW-1:0]       merge_count
);

    logic [MSHR_SIZE-1:0] valid_table_r;
    logic [MSHR_SIZE-1:0] link_valid_r;
    logic [ADDRW-1:0] addr_table_r [0:MSHR_SIZE-1];
    logic [DATAW-1:0] data_table_r [0:MSHR_SIZE-1];
    logic [3:0] mask_table_r [0:MSHR_SIZE-1];
    logic [7:0] tag_table_r [0:MSHR_SIZE-1];
    logic write_table_r [0:MSHR_SIZE-1];
    logic [IDW-1:0] next_table_r [0:MSHR_SIZE-1];

    logic free_found;
    logic [IDW-1:0] free_index;
    logic match_found;
    logic [IDW-1:0] match_head;
    logic tail_found;
    logic [IDW-1:0] match_tail;
    logic [MSHR_SIZE-1:0] address_matches;

    logic replay_active_r;
    logic [IDW-1:0] replay_index_r;
    logic delayed_dequeue_valid_r;
    logic invalid_fill_seen_r;
    logic invalid_finalize_seen_r;
    logic allocation_collision_seen_r;

    logic [IDW:0] occupancy_comb;
    logic [IDW:0] chain_length_comb;
    logic [IDW:0] high_watermark_r;

    integer scan;
    integer count_index;

    wire allocate_fire;
    wire dequeue_fire;
    wire replay_has_next;
    wire release_current;

    initial begin
        if (MSHR_SIZE < 2) begin
            $fatal(1, "MSHR_SIZE must be at least two");
        end
        if ((1 << IDW) < MSHR_SIZE) begin
            $fatal(1, "IDW cannot address every MSHR entry");
        end
        if (ADDRW < 2) begin
            $fatal(1, "ADDRW must be at least two");
        end
    end

    // Parallel CAM lookup finds all active requests for the incoming line.
    always_comb begin
        for (scan = 0; scan < MSHR_SIZE; scan = scan + 1) begin
            address_matches[scan] = valid_table_r[scan]
                                    && (addr_table_r[scan] == allocate_addr);
        end
    end

    // Lowest numbered free entry is chosen deterministically.
    always_comb begin
        free_found = 1'b0;
        free_index = '0;
        for (scan = MSHR_SIZE - 1; scan >= 0; scan = scan - 1) begin
            if (!valid_table_r[scan]) begin
                free_found = 1'b1;
                free_index = IDW'(scan);
            end
        end
    end

    // Find both the chain head and the current tail for an address match.
    always_comb begin
        match_found = 1'b0;
        match_head = '0;
        tail_found = 1'b0;
        match_tail = '0;
        for (scan = MSHR_SIZE - 1; scan >= 0; scan = scan - 1) begin
            if (address_matches[scan]) begin
                match_found = 1'b1;
                match_head = IDW'(scan);
                if (!link_valid_r[scan]) begin
                    tail_found = 1'b1;
                    match_tail = IDW'(scan);
                end
            end
        end
    end

    always_comb begin
        occupancy_comb = '0;
        for (count_index = 0;
             count_index < MSHR_SIZE;
             count_index = count_index + 1) begin
            occupancy_comb = occupancy_comb + valid_table_r[count_index];
        end
    end

    // Chain length is diagnostic but also checks that traversal is bounded.
    always_comb begin
        chain_length_comb = '0;
        for (count_index = 0;
             count_index < MSHR_SIZE;
             count_index = count_index + 1) begin
            if (address_matches[count_index]) begin
                chain_length_comb = chain_length_comb + 1'b1;
            end
        end
    end

    assign allocate_ready = free_found && !flush;
    assign allocate_id = free_index;
    assign allocate_pending = match_found;
    assign allocate_previd = match_tail;
    assign allocate_fire = allocate_valid && allocate_ready;

    assign dequeue_fire = dequeue_valid && dequeue_ready;
    assign replay_has_next = link_valid_r[replay_index_r];
    assign release_current = dequeue_fire;

    assign fill_addr = addr_table_r[fill_id];
    assign dequeue_addr = addr_table_r[replay_index_r];
    assign dequeue_write = write_table_r[replay_index_r];
    assign dequeue_data = data_table_r[replay_index_r];
    assign dequeue_mask = mask_table_r[replay_index_r];
    assign dequeue_tag = tag_table_r[replay_index_r];
    assign dequeue_id = replay_index_r;
    assign valid_entries = valid_table_r;
    assign occupancy = occupancy_comb;
    assign full = &valid_table_r;

    // Main table update supports allocation, finalization, fill, and replay.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            valid_table_r <= '0;
            link_valid_r <= '0;
            replay_active_r <= 1'b0;
            replay_index_r <= '0;
            for (scan = 0; scan < MSHR_SIZE; scan = scan + 1) begin
                addr_table_r[scan] <= '0;
                data_table_r[scan] <= '0;
                mask_table_r[scan] <= '0;
                tag_table_r[scan] <= '0;
                write_table_r[scan] <= 1'b0;
                next_table_r[scan] <= '0;
            end
        end else begin
            if (release_current) begin
                valid_table_r[replay_index_r] <= 1'b0;
                link_valid_r[replay_index_r] <= 1'b0;
                if (replay_has_next) begin
                    replay_index_r <= next_table_r[replay_index_r];
                    replay_active_r <= 1'b1;
                end else begin
                    replay_active_r <= 1'b0;
                end
            end

            if (finalize_valid) begin
                if (finalize_release) begin
                    valid_table_r[finalize_id] <= 1'b0;
                    link_valid_r[finalize_id] <= 1'b0;
                end
                if (finalize_pending) begin
                    link_valid_r[finalize_previd] <= 1'b1;
                    next_table_r[finalize_previd] <= finalize_id;
                end
            end

            if (allocate_fire) begin
                valid_table_r[free_index] <= 1'b1;
                link_valid_r[free_index] <= 1'b0;
                addr_table_r[free_index] <= allocate_addr;
                data_table_r[free_index] <= allocate_data;
                mask_table_r[free_index] <= allocate_mask;
                tag_table_r[free_index] <= allocate_tag;
                write_table_r[free_index] <= allocate_write;
                next_table_r[free_index] <= '0;
            end

            if (fill_valid) begin
                replay_active_r <= valid_table_r[fill_id];
                replay_index_r <= fill_id;
            end
        end
    end

    // Event counters expose sustained behavior without observing internals.
    always_ff @(posedge clk) begin
        if (reset) begin
            allocation_count <= '0;
            replay_count <= '0;
            merge_count <= '0;
            high_watermark_r <= '0;
        end else begin
            if (allocate_fire) begin
                allocation_count <= allocation_count + 1'b1;
                if (allocate_pending) begin
                    merge_count <= merge_count + 1'b1;
                end
            end
            if (dequeue_fire) begin
                replay_count <= replay_count + 1'b1;
            end
            if (occupancy_comb > high_watermark_r) begin
                high_watermark_r <= occupancy_comb;
            end
        end
    end

    // Protocol diagnostics remember illegal operations for waveform debug.
    always_ff @(posedge clk) begin
        if (reset) begin
            invalid_fill_seen_r <= 1'b0;
            invalid_finalize_seen_r <= 1'b0;
            allocation_collision_seen_r <= 1'b0;
        end else begin
            if (fill_valid && !valid_table_r[fill_id]) begin
                invalid_fill_seen_r <= 1'b1;
            end
            if (finalize_valid && !valid_table_r[finalize_id]) begin
                invalid_finalize_seen_r <= 1'b1;
            end
            if (allocate_fire && valid_table_r[free_index]) begin
                allocation_collision_seen_r <= 1'b1;
            end
        end
    end

    // The mutation delays visibility of the replay stream by one clock.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            delayed_dequeue_valid_r <= 1'b0;
        end else begin
            delayed_dequeue_valid_r <= replay_active_r;
        end
    end

    assign dequeue_valid = TIMING_BUG
                         ? delayed_dequeue_valid_r
                         : replay_active_r;

endmodule

`default_nettype wire
