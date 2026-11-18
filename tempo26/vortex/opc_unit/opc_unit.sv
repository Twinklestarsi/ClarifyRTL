`timescale 1ns / 1ps
`default_nettype none

// Standalone banked operand collector and multi-context vector register file.
module VX_opc_unit #(
    parameter integer DATAW        = 32,
    parameter integer NUM_LANES    = 4,
    parameter integer NUM_REGS     = 32,
    parameter integer NUM_CONTEXTS = 4,
    parameter integer NUM_BANKS    = 4,
    parameter integer TAGW         = 8,
    parameter integer PCW          = 32,
    parameter integer OPW          = 8,
    parameter integer TIMING_BUG   = 0,
    parameter integer REGW         = $clog2(NUM_REGS),
    parameter integer CTXW         = $clog2(NUM_CONTEXTS),
    parameter integer COLLISIONW   = 16
) (
    input  wire                         clk,
    input  wire                         reset,

    input  wire                         wb_valid,
    input  wire [CTXW-1:0]              wb_context,
    input  wire [REGW-1:0]              wb_rd,
    input  wire [NUM_LANES-1:0]         wb_tmask,
    input  wire [NUM_LANES*DATAW-1:0]   wb_data,

    input  wire                         issue_valid,
    output wire                         issue_ready,
    input  wire [2:0]                   issue_used_rs,
    input  wire [REGW-1:0]              issue_rs1,
    input  wire [REGW-1:0]              issue_rs2,
    input  wire [REGW-1:0]              issue_rs3,
    input  wire [CTXW-1:0]              issue_context,
    input  wire [NUM_LANES-1:0]         issue_tmask,
    input  wire [TAGW-1:0]              issue_tag,
    input  wire [PCW-1:0]               issue_pc,
    input  wire [OPW-1:0]               issue_op,
    input  wire [REGW-1:0]              issue_rd,
    input  wire                         issue_wb,
    input  wire                         issue_sop,
    input  wire                         issue_eop,

    output wire                         operands_valid,
    input  wire                         operands_ready,
    output wire [NUM_LANES*DATAW-1:0]   operand1,
    output wire [NUM_LANES*DATAW-1:0]   operand2,
    output wire [NUM_LANES*DATAW-1:0]   operand3,
    output wire [CTXW-1:0]              operand_context,
    output wire [NUM_LANES-1:0]         operand_tmask,
    output wire [TAGW-1:0]              operand_tag,
    output wire [PCW-1:0]               operand_pc,
    output wire [OPW-1:0]               operand_op,
    output wire [REGW-1:0]              operand_rd,
    output wire                         operand_wb,
    output wire                         operand_sop,
    output wire                         operand_eop,
    output wire [COLLISIONW-1:0]        collision_count
);

    localparam integer VECTORW = NUM_LANES * DATAW;
    localparam integer TOTAL_REGS = NUM_CONTEXTS * NUM_REGS;

    typedef logic [VECTORW-1:0] vector_t;

    vector_t gpr_mem [0:TOTAL_REGS-1];

    logic collect_valid_r;
    logic [2:0] fetched_r;
    logic [REGW-1:0] src_regs_r [0:2];
    vector_t operand_work_r [0:2];
    logic [CTXW-1:0] context_work_r;
    logic [NUM_LANES-1:0] tmask_work_r;
    logic [TAGW-1:0] tag_work_r;
    logic [PCW-1:0] pc_work_r;
    logic [OPW-1:0] op_work_r;
    logic [REGW-1:0] rd_work_r;
    logic wb_work_r;
    logic sop_work_r;
    logic eop_work_r;
    logic delay_publish_r;

    logic [2:0] fetched_next;
    vector_t operand_next [0:2];
    logic [NUM_BANKS-1:0] bank_used;
    logic collision_now;

    vector_t fifo_op1 [0:1];
    vector_t fifo_op2 [0:1];
    vector_t fifo_op3 [0:1];
    logic [CTXW-1:0] fifo_context [0:1];
    logic [NUM_LANES-1:0] fifo_tmask [0:1];
    logic [TAGW-1:0] fifo_tag [0:1];
    logic [PCW-1:0] fifo_pc [0:1];
    logic [OPW-1:0] fifo_op [0:1];
    logic [REGW-1:0] fifo_rd [0:1];
    logic fifo_wb [0:1];
    logic fifo_sop [0:1];
    logic fifo_eop [0:1];
    logic fifo_wr_ptr_r;
    logic fifo_rd_ptr_r;
    logic [1:0] fifo_count_r;

    logic [COLLISIONW-1:0] collision_count_r;

    integer src;
    integer prior;
    integer lane;
    integer bank_index;
    integer mem_addr;
    integer reset_index;

    wire fifo_pop = (fifo_count_r != 0) && operands_ready;
    wire fifo_space = (fifo_count_r != 2) || fifo_pop;
    wire all_fetched_next = &fetched_next;
    wire publish_candidate = collect_valid_r && all_fetched_next;
    wire publish_delay_done = !TIMING_BUG || delay_publish_r;
    wire fifo_push = publish_candidate && publish_delay_done && fifo_space;

    assign issue_ready = !collect_valid_r;
    wire issue_fire = issue_valid && issue_ready;

    assign operands_valid   = (fifo_count_r != 0);
    assign operand1         = fifo_op1[fifo_rd_ptr_r];
    assign operand2         = fifo_op2[fifo_rd_ptr_r];
    assign operand3         = fifo_op3[fifo_rd_ptr_r];
    assign operand_context  = fifo_context[fifo_rd_ptr_r];
    assign operand_tmask    = fifo_tmask[fifo_rd_ptr_r];
    assign operand_tag      = fifo_tag[fifo_rd_ptr_r];
    assign operand_pc       = fifo_pc[fifo_rd_ptr_r];
    assign operand_op       = fifo_op[fifo_rd_ptr_r];
    assign operand_rd       = fifo_rd[fifo_rd_ptr_r];
    assign operand_wb       = fifo_wb[fifo_rd_ptr_r];
    assign operand_sop      = fifo_sop[fifo_rd_ptr_r];
    assign operand_eop      = fifo_eop[fifo_rd_ptr_r];
    assign collision_count  = collision_count_r;

    function automatic integer register_address;
        input [CTXW-1:0] ctx;
        input [REGW-1:0] reg_number;
        begin
            register_address = ctx * NUM_REGS + reg_number;
        end
    endfunction

    function automatic integer register_bank;
        input [REGW-1:0] reg_number;
        begin
            register_bank = reg_number % NUM_BANKS;
        end
    endfunction

    always @(*) begin
        fetched_next = fetched_r;
        operand_next[0] = operand_work_r[0];
        operand_next[1] = operand_work_r[1];
        operand_next[2] = operand_work_r[2];
        bank_used = '0;
        collision_now = 1'b0;

        if (collect_valid_r) begin
            for (src = 0; src < 3; src = src + 1) begin
                if (!fetched_r[src]) begin
                    bank_index = register_bank(src_regs_r[src]);
                    if (!bank_used[bank_index]) begin
                        bank_used[bank_index] = 1'b1;
                        fetched_next[src] = 1'b1;
                        mem_addr = register_address(context_work_r, src_regs_r[src]);
                        operand_next[src] = gpr_mem[mem_addr];
                        if (wb_valid
                         && wb_context == context_work_r
                         && wb_rd == src_regs_r[src]
                         && wb_rd != 0) begin
                            for (lane = 0; lane < NUM_LANES; lane = lane + 1) begin
                                if (wb_tmask[lane]) begin
                                    operand_next[src][lane*DATAW +: DATAW]
                                        = wb_data[lane*DATAW +: DATAW];
                                end
                            end
                        end
                    end else begin
                        collision_now = 1'b1;
                    end
                end
            end

            for (src = 0; src < 3; src = src + 1) begin
                if (!fetched_r[src]) begin
                    for (prior = 0; prior < src; prior = prior + 1) begin
                        if (!fetched_r[prior]
                         && register_bank(src_regs_r[src])
                            == register_bank(src_regs_r[prior])) begin
                            collision_now = 1'b1;
                        end
                    end
                end
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            for (reset_index = 0; reset_index < TOTAL_REGS; reset_index = reset_index + 1)
                gpr_mem[reset_index] <= '0;
        end else if (wb_valid && wb_rd != 0) begin
            for (lane = 0; lane < NUM_LANES; lane = lane + 1) begin
                if (wb_tmask[lane]) begin
                    gpr_mem[register_address(wb_context, wb_rd)]
                           [lane*DATAW +: DATAW]
                        <= wb_data[lane*DATAW +: DATAW];
                end
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            collect_valid_r <= 1'b0;
            fetched_r <= '0;
            src_regs_r[0] <= '0;
            src_regs_r[1] <= '0;
            src_regs_r[2] <= '0;
            operand_work_r[0] <= '0;
            operand_work_r[1] <= '0;
            operand_work_r[2] <= '0;
            context_work_r <= '0;
            tmask_work_r <= '0;
            tag_work_r <= '0;
            pc_work_r <= '0;
            op_work_r <= '0;
            rd_work_r <= '0;
            wb_work_r <= 1'b0;
            sop_work_r <= 1'b0;
            eop_work_r <= 1'b0;
            delay_publish_r <= 1'b0;
        end else begin
            if (issue_fire) begin
                collect_valid_r <= 1'b1;
                fetched_r[0] <= !issue_used_rs[0] || (issue_rs1 == 0);
                fetched_r[1] <= !issue_used_rs[1] || (issue_rs2 == 0);
                fetched_r[2] <= !issue_used_rs[2] || (issue_rs3 == 0);
                src_regs_r[0] <= issue_rs1;
                src_regs_r[1] <= issue_rs2;
                src_regs_r[2] <= issue_rs3;
                operand_work_r[0] <= '0;
                operand_work_r[1] <= '0;
                operand_work_r[2] <= '0;
                context_work_r <= issue_context;
                tmask_work_r <= issue_tmask;
                tag_work_r <= issue_tag;
                pc_work_r <= issue_pc;
                op_work_r <= issue_op;
                rd_work_r <= issue_rd;
                wb_work_r <= issue_wb;
                sop_work_r <= issue_sop;
                eop_work_r <= issue_eop;
                delay_publish_r <= 1'b0;
            end else if (collect_valid_r) begin
                fetched_r <= fetched_next;
                operand_work_r[0] <= operand_next[0];
                operand_work_r[1] <= operand_next[1];
                operand_work_r[2] <= operand_next[2];
                if (publish_candidate && TIMING_BUG && !delay_publish_r)
                    delay_publish_r <= 1'b1;
                if (fifo_push) begin
                    collect_valid_r <= 1'b0;
                    delay_publish_r <= 1'b0;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            fifo_wr_ptr_r <= 1'b0;
            fifo_rd_ptr_r <= 1'b0;
            fifo_count_r <= '0;
            fifo_op1[0] <= '0;
            fifo_op1[1] <= '0;
            fifo_op2[0] <= '0;
            fifo_op2[1] <= '0;
            fifo_op3[0] <= '0;
            fifo_op3[1] <= '0;
            fifo_context[0] <= '0;
            fifo_context[1] <= '0;
            fifo_tmask[0] <= '0;
            fifo_tmask[1] <= '0;
            fifo_tag[0] <= '0;
            fifo_tag[1] <= '0;
            fifo_pc[0] <= '0;
            fifo_pc[1] <= '0;
            fifo_op[0] <= '0;
            fifo_op[1] <= '0;
            fifo_rd[0] <= '0;
            fifo_rd[1] <= '0;
            fifo_wb[0] <= 1'b0;
            fifo_wb[1] <= 1'b0;
            fifo_sop[0] <= 1'b0;
            fifo_sop[1] <= 1'b0;
            fifo_eop[0] <= 1'b0;
            fifo_eop[1] <= 1'b0;
        end else begin
            if (fifo_push) begin
                fifo_op1[fifo_wr_ptr_r] <= operand_next[0];
                fifo_op2[fifo_wr_ptr_r] <= operand_next[1];
                fifo_op3[fifo_wr_ptr_r] <= operand_next[2];
                fifo_context[fifo_wr_ptr_r] <= context_work_r;
                fifo_tmask[fifo_wr_ptr_r] <= tmask_work_r;
                fifo_tag[fifo_wr_ptr_r] <= tag_work_r;
                fifo_pc[fifo_wr_ptr_r] <= pc_work_r;
                fifo_op[fifo_wr_ptr_r] <= op_work_r;
                fifo_rd[fifo_wr_ptr_r] <= rd_work_r;
                fifo_wb[fifo_wr_ptr_r] <= wb_work_r;
                fifo_sop[fifo_wr_ptr_r] <= sop_work_r;
                fifo_eop[fifo_wr_ptr_r] <= eop_work_r;
                fifo_wr_ptr_r <= ~fifo_wr_ptr_r;
            end
            if (fifo_pop)
                fifo_rd_ptr_r <= ~fifo_rd_ptr_r;
            case ({fifo_push, fifo_pop})
                2'b10: fifo_count_r <= fifo_count_r + 1'b1;
                2'b01: fifo_count_r <= fifo_count_r - 1'b1;
                default: fifo_count_r <= fifo_count_r;
            endcase
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            collision_count_r <= '0;
        end else if (collect_valid_r && collision_now) begin
            collision_count_r <= collision_count_r + 1'b1;
        end
    end

    initial begin
        if (DATAW < 1 || NUM_LANES < 1)
            $fatal(1, "data geometry must be positive");
        if (NUM_REGS < 2 || NUM_CONTEXTS < 1)
            $fatal(1, "register geometry is invalid");
        if (NUM_BANKS < 1 || NUM_BANKS > NUM_REGS)
            $fatal(1, "NUM_BANKS is out of range");
    end

endmodule

`default_nettype wire
